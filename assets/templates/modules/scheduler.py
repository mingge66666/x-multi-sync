import os
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
        accounts = [
            a.lstrip("@")
            for a in (config.get("monitor_accounts") or [])
            if a
        ]
    return accounts


def get_target_user_id(tweets):
    for t in tweets:
        core = t.get("core", {})
        uid = core.get("user_results", {}).get("result", {}).get("rest_id", "")
        if uid:
            return uid
    return None


def cursor_key(username: str) -> str:
    return f"last_tweet_id:{username}"


async def collect_from_account(config, username: str, stats: dict):
    """从单个监控号采集并发到自己的 X。"""
    logger.info(f"—— 采集监控号 @{username} ——")
    last_id = db.get_sync_state(cursor_key(username))
    raw, auth_error = await fetcher.fetch_tweets(config, username=username)

    if auth_error:
        notifier.send_alert(
            f"抓取 @{username} 失败（Cookie/网络）：{auth_error}",
            "CRITICAL",
        )
        stats["errors"].append(f"{username}:{auth_error}")
        return

    stats["fetched_count"] += len(raw)
    if not raw:
        logger.info(f"@{username} 无数据")
        return

    target_uid = get_target_user_id(raw) or ""
    highest = last_id
    today = datetime.now(UTC8).strftime("%Y-%m-%d")
    batch = []

    for tweet in raw:
        tid = tweet.get("rest_id", "")
        if not tid:
            continue
        if last_id and int(tid) <= int(last_id):
            continue
        if not last_id:
            created = tweet.get("legacy", {}).get("created_at", "")
            if created:
                try:
                    dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
                    if dt.astimezone(UTC8).strftime("%Y-%m-%d") != today:
                        continue
                except ValueError:
                    pass
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
            logger.warning("发 X 失败或触达日限额，停止本轮后续发帖")
            break

    if highest:
        db.set_sync_state(cursor_key(username), highest)


async def distribute_from_my_x(config, stats: dict):
    """从自己的 X 原样同步到币安 + TG（仅原创）。"""
    my = config.get("my_x") or {}
    username = (my.get("username") or "").lstrip("@")
    if not username:
        logger.warning("未配置 my_x.username，跳过分发")
        return

    logger.info(f"—— 分发主号 @{username} → 广场/TG ——")
    halt = db.get_sync_state("halt_publishing_today") == "1"
    last_id = db.get_sync_state(cursor_key(f"hub:{username}"))
    cookies = my.get("cookies_path", "./cookies/my_x.json")

    raw, auth_error = await fetcher.fetch_tweets(
        config,
        username=username,
        cookies_path=cookies,
        profile_url=my.get("profile_url") or f"https://x.com/{username}",
    )
    if auth_error:
        notifier.send_alert(f"主号时间线抓取失败：{auth_error}", "CRITICAL")
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
        if not highest or int(tid) > int(highest):
            highest = tid

        # 主号：严格原创，但不再做引流过滤
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
        logger.info("今日广场已暂停，仅更新游标")
        if highest:
            db.set_sync_state(cursor_key(f"hub:{username}"), highest)
        return

    api = config["binance_square"]["api_endpoint"]
    delay = config["binance_square"]["inter_request_delay_seconds"]

    for data in batch:
        db.save_tweet(data)
        success, result = await publisher.publish_to_square(
            data["content_clean"],
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
                    data["content_clean"],
                    result.get("square_post_url", ""),
                )
            except Exception as e:
                logger.warning(f"TG 同步失败: {e}")
        else:
            if result.get("halt_today"):
                db.set_sync_state("halt_publishing_today", "1")
                notifier.send_alert("币安广场今日额度已满。", "WARNING")
                break
            if result.get("halt_permanent"):
                notifier.send_alert("币安 API Key 异常。", "CRITICAL")
                break
            stats["failed_count"] += 1
        await asyncio.sleep(delay)

    if highest:
        db.set_sync_state(cursor_key(f"hub:{username}"), highest)


async def sync_cycle(config):
    run_start = datetime.now(UTC8)
    logger.info("=" * 50)
    logger.info(f"总周期开始 {run_start.isoformat()}")

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
        logger.warning("accounts.txt 为空且未配置 monitor_accounts")
        notifier.send_alert("监控账号列表为空，请编辑 accounts.txt", "WARNING")
    else:
        logger.info(f"监控账号：{accounts}")
        delay = int(config.get("scheduler", {}).get("per_account_delay_seconds", 20))
        for i, user in enumerate(accounts):
            try:
                await collect_from_account(config, user, stats)
            except Exception as e:
                logger.exception(f"采集 @{user} 异常: {e}")
                stats["errors"].append(str(e))
            if i < len(accounts) - 1:
                await asyncio.sleep(delay)

    try:
        await distribute_from_my_x(config, stats)
    except Exception as e:
        logger.exception(f"分发异常: {e}")
        notifier.send_alert(f"分发异常：{e}", "ERROR")

    duration = (datetime.now(UTC8) - run_start).total_seconds()
    stats["duration_seconds"] = duration
    db.save_run_log(stats)
    db.set_sync_state("last_run_at", datetime.now(UTC8).isoformat())
    logger.info(
        f"总周期结束 {duration:.1f}s | 抓取={stats['fetched_count']} "
        f"发X={stats['published_count']} 广场={stats.get('distributed', 0)} "
        f"失败={stats['failed_count']}"
    )


def daily_digest():
    notifier.send_daily_digest(db.get_daily_stats())


def midnight_reset():
    today = datetime.now(UTC8).strftime("%Y-%m-%d")
    db.set_sync_state("halt_publishing_today", "0")
    db.set_sync_state("daily_count_date", today)
    logger.info(f"午夜重置 {today}")
