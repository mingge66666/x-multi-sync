import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from loguru import logger

from modules import db, fetcher, filter as tweet_filter, publisher, notifier, telegram_sync
from modules.x_publisher import post_to_x

UTC8 = timezone(timedelta(hours=8))


def load_monitor_accounts(config):
    path = config.get("accounts_file", "./accounts.txt")
    accounts = []
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            accounts.append(line.lstrip("@"))
    if not accounts:
        accounts = [a.lstrip("@") for a in (config.get("monitor_accounts") or []) if a]
    return accounts


def get_target_user_id(tweets):
    for t in tweets:
        core = t.get("core", {})
        uid = core.get("user_results", {}).get("result", {}).get("rest_id", "")
        if uid:
            return uid
    return None


def cursor_key(name: str) -> str:
    return f"last_tweet_id:{name}"


def is_within_hours(tweet, hours=1.0) -> bool:
    """推文是否在最近 hours 小时内发布。"""
    created_at = tweet.get("legacy", {}).get("created_at", "")
    if not created_at:
        return False
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age <= timedelta(hours=float(hours))
    except ValueError:
        return False


def max_age_hours(config) -> float:
    return float(config.get("scheduler", {}).get("max_tweet_age_hours", 1))


async def collect_from_account(config, username: str, stats: dict):
    """从单个监控号采集：过滤 → 中文 → 发到自己的 X（图文；无视频/纯文字/推广）。"""
    logger.info(f"—— 开始采集监控号 @{username} ——")
    last_id = db.get_sync_state(cursor_key(username))
    raw, auth_error = await fetcher.fetch_tweets(config, username=username)
    hours = max_age_hours(config)

    if auth_error:
        notifier.send_alert(
            f"抓取监控号 @{username} 失败。\n原因：{auth_error}\n请检查 cookies/twitter.json 是否有效。",
            "CRITICAL",
        )
        stats["errors"].append(f"{username}:{auth_error}")
        return

    stats["fetched_count"] += len(raw)
    if not raw:
        logger.info(f"@{username} 本周期未抓到推文")
        return

    target_uid = get_target_user_id(raw) or ""
    highest = last_id
    batch = []

    for tweet in raw:
        tid = tweet.get("rest_id", "")
        if not tid:
            continue
        if last_id and int(tid) <= int(last_id):
            continue
        if not is_within_hours(tweet, hours=hours):
            continue
        if not highest or int(tid) > int(highest):
            highest = tid

        ok, result, data = tweet_filter.filter_tweet(
            tweet, target_uid, max_content_length=280, strict_promo=True
        )
        if not ok:
            if result != "dedup":
                data["status"] = "skipped"
                data["filter_result"] = result
                db.save_tweet(data)
                stats["skipped_count"] += 1
            continue
        stats["passed_filter"] += 1
        data["source_account"] = username
        batch.append(data)

    batch.sort(key=lambda x: int(x["tweet_id"]))
    my_cookies = config.get("my_x", {}).get("cookies_path", "./cookies/my_x.json")

    for data in batch:
        db.save_tweet(data)
        posted = await post_to_x(
            data.get("content_clean", ""),
            my_cookies,
            image_urls=data.get("image_urls") or [],
        )
        if posted:
            db.update_tweet_status(
                data["tweet_id"],
                status="published",
                published_at=datetime.now(UTC8).isoformat(),
            )
            stats["published_count"] += 1
        else:
            db.update_tweet_status(data["tweet_id"], status="failed")
            stats["failed_count"] += 1
            notifier.send_alert(
                f"发布到自己的 X 失败或已达日限额，本轮停止继续发帖。\n推文 ID：{data['tweet_id']}",
                "WARNING",
            )
            break

    if highest:
        db.set_sync_state(cursor_key(username), highest)
    logger.info(f"—— 结束采集 @{username} ——")


async def distribute_from_my_x(config, stats: dict):
    """从自己的 X 同步到币安广场 + Telegram（近 N 小时；$TOKEN 大写）。"""
    my = config.get("my_x") or {}
    username = (my.get("username") or "").lstrip("@")
    if not username:
        logger.warning("未配置 my_x.username，跳过广场/频道分发")
        return

    logger.info(f"—— 开始分发主号 @{username} ——")
    halt = db.get_sync_state("halt_publishing_today") == "1"
    last_id = db.get_sync_state(cursor_key(f"hub:{username}"))
    cookies = my.get("cookies_path", "./cookies/my_x.json")
    hours = max_age_hours(config)

    raw, auth_error = await fetcher.fetch_tweets(
        config,
        username=username,
        cookies_path=cookies,
        profile_url=my.get("profile_url") or f"https://x.com/{username}",
    )
    if auth_error:
        notifier.send_alert(
            f"读取主号 @{username} 时间线失败。\n原因：{auth_error}\n请更新 cookies/my_x.json。",
            "CRITICAL",
        )
        return

    stats["hub_fetched"] = len(raw)
    target_uid = get_target_user_id(raw) or ""
    highest = last_id
    max_len = config.get("binance_square", {}).get("max_content_length", 900)
    batch = []

    for tweet in raw:
        tid = tweet.get("rest_id", "")
        if not tid:
            continue
        if last_id and int(tid) <= int(last_id):
            continue
        if not is_within_hours(tweet, hours=hours):
            continue
        if not highest or int(tid) > int(highest):
            highest = tid

        ok, result, data = tweet_filter.filter_tweet(
            tweet, target_uid, max_content_length=max_len, strict_promo=False
        )
        if not ok:
            if result != "dedup":
                stats["skipped_count"] += 1
            continue
        batch.append(data)

    batch.sort(key=lambda x: int(x["tweet_id"]))

    if halt:
        logger.info("今日币安发帖已暂停，仅更新游标")
        if highest:
            db.set_sync_state(cursor_key(f"hub:{username}"), highest)
        return

    api = config["binance_square"]["api_endpoint"]
    delay = config["binance_square"]["inter_request_delay_seconds"]

    for data in batch:
        db.save_tweet(data)

        # 广场与 TG 同一正文：$METIS 大写 + 空格（由 filter/processor 生成）
        text_body = (
            data.get("content_square")
            or data.get("content_clean")
            or data.get("content_raw")
            or ""
        )

        success, result = await publisher.publish_to_square(
            text_body,
            api,
            delay,
            image_urls=data.get("image_urls") or [],
        )
        if success:
            db.update_tweet_status(
                data["tweet_id"],
                status="published",
                published_at=datetime.now(UTC8).isoformat(),
                square_post_id=result.get("square_post_id", ""),
                square_post_url=result.get("square_post_url", ""),
            )
            stats["distributed"] = stats.get("distributed", 0) + 1
            try:
                await telegram_sync.send_sync_post(
                    text_body,
                    square_url="",
                    image_urls=data.get("image_urls") or [],
                )
            except Exception as e:
                logger.warning(f"同步公开频道失败: {e}")
        else:
            if result.get("halt_today"):
                db.set_sync_state("halt_publishing_today", "1")
                notifier.send_alert(
                    "币安广场已达今日发帖上限，已暂停分发，将于次日自动恢复。",
                    "WARNING",
                )
                break
            if result.get("halt_permanent"):
                notifier.send_alert(
                    f"币安广场 API Key 异常（{result.get('error_code')}），分发已停止，请检查密钥。",
                    "CRITICAL",
                )
                break
            if result.get("skip_permanent"):
                notifier.send_alert(
                    f"推文 {data['tweet_id']} 被广场判定敏感，已跳过。",
                    "WARNING",
                )
                stats["skipped_count"] += 1
            else:
                stats["failed_count"] += 1
        await asyncio.sleep(delay)

    if highest:
        db.set_sync_state(cursor_key(f"hub:{username}"), highest)
    logger.info("—— 分发阶段结束 ——")


async def sync_cycle(config):
    run_start = datetime.now(UTC8)
    logger.info("=" * 50)
    logger.info(f"总同步周期开始于 {run_start.isoformat()}")

    stats = {
        "run_at": run_start.isoformat(),
        "fetched_count": 0,
        "passed_filter": 0,
        "published_count": 0,
        "distributed": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "errors": [],
    }

    await notifier.flush_queue()

    accounts = load_monitor_accounts(config)
    if not accounts:
        logger.warning("监控账号列表为空")
        notifier.send_alert(
            "监控账号列表为空，请编辑 accounts.txt 后重启服务。",
            "WARNING",
        )
    else:
        logger.info(f"当前监控账号：{', '.join('@' + a for a in accounts)}")
        logger.info(f"仅处理最近 {max_age_hours(config)} 小时内的推文")
        delay = int(config.get("scheduler", {}).get("per_account_delay_seconds", 20))
        for i, user in enumerate(accounts):
            try:
                await collect_from_account(config, user, stats)
            except Exception as e:
                logger.exception(f"采集 @{user} 时异常: {e}")
                notifier.send_alert(f"采集 @{user} 时发生异常：{e}", "ERROR")
                stats["errors"].append(str(e))
            if i < len(accounts) - 1:
                await asyncio.sleep(delay)

    try:
        await distribute_from_my_x(config, stats)
    except Exception as e:
        logger.exception(f"分发阶段异常: {e}")
        notifier.send_alert(f"分发到广场/频道时异常：{e}", "ERROR")

    duration = (datetime.now(UTC8) - run_start).total_seconds()
    stats["duration_seconds"] = duration
    db.save_run_log(stats)
    db.set_sync_state("last_run_at", datetime.now(UTC8).isoformat())
    logger.info(
        f"总周期结束，耗时 {duration:.1f} 秒 | "
        f"抓取={stats['fetched_count']} 通过过滤={stats['passed_filter']} "
        f"发到X={stats['published_count']} 发到广场={stats.get('distributed', 0)} "
        f"失败={stats['failed_count']} 跳过={stats['skipped_count']}"
    )


def daily_digest():
    notifier.send_daily_digest(db.get_daily_stats())
    logger.info("每日报告已发送")


def midnight_reset():
    today = datetime.now(UTC8).strftime("%Y-%m-%d")
    db.set_sync_state("halt_publishing_today", "0")
    db.set_sync_state("daily_count_date", today)
    logger.info(f"已执行午夜重置，日期={today}")
