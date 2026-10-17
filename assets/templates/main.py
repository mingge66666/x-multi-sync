import os
import sys
import stat
import signal
import asyncio
import yaml
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from modules import db, notifier
from modules.scheduler import sync_cycle, daily_digest, midnight_reset

UTC8 = timezone(timedelta(hours=8))


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        print(f"[致命] 未找到 config.yaml：{config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_env():
    if not os.getenv("BINANCE_SQUARE_API_KEY"):
        print("[致命] 未配置 BINANCE_SQUARE_API_KEY，请编辑 .env")
        sys.exit(1)
    if not os.getenv("TELEGRAM_OPS_BOT_TOKEN") or not os.getenv("TELEGRAM_OPS_CHAT_ID"):
        print("[警告] 未配置运维 Telegram，告警将不会发送")
    if not os.getenv("TELEGRAM_SYNC_BOT_TOKEN") or not os.getenv("TELEGRAM_SYNC_CHAT_ID"):
        print("[警告] 未配置公开频道 Telegram，分发到频道将跳过")
    if not os.getenv("DEEPL_API_KEY"):
        print("[警告] 未配置 DEEPL_API_KEY，英文推文可能不会翻译")


def setup_logging(config):
    log_config = config.get("logging", {})
    log_file = log_config.get("file", "./logs/sync.log")
    log_level = log_config.get("level", "INFO")
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=log_level, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    logger.add(
        log_file,
        level=log_level,
        rotation=log_config.get("max_bytes", 52428800),
        retention=log_config.get("backup_count", 5),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {module}:{function}:{line} | {message}",
        encoding="utf-8",
    )


def ensure_dirs():
    for d in ["db", "logs", "tmp", "cookies"]:
        os.makedirs(d, exist_ok=True)


def enforce_cookie_permissions(config):
    for key in (
        config.get("twitter", {}).get("cookies_path"),
        config.get("my_x", {}).get("cookies_path"),
    ):
        if not key or not os.path.exists(key):
            continue
        try:
            mode = os.stat(key).st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                os.chmod(key, 0o600)
                logger.warning(f"已收紧 Cookie 权限为 600：{key}")
        except PermissionError:
            logger.warning(f"无权限修改 Cookie 文件权限，已跳过：{key}")
        except Exception as e:
            logger.warning(f"检查 Cookie 权限时出错：{e}")


async def run_sync_cycle(config):
    try:
        await sync_cycle(config)
    except Exception as e:
        logger.exception(f"同步周期异常退出: {e}")
        notifier.send_alert(f"同步周期异常退出：{e}", "ERROR")


async def test_mode(config):
    logger.info("=== 测试模式（只抓取过滤，不发帖）===")
    notifier.send_alert("测试模式已启动，这是一条连通性测试。", "INFO")
    print("[信息] 请到运维 Telegram 频道确认是否收到测试消息")

    from modules import fetcher, filter as tweet_filter
    from modules.scheduler import load_monitor_accounts

    accounts = load_monitor_accounts(config)
    if not accounts:
        print("[失败] accounts.txt 中没有监控账号")
        return
    user = accounts[0]
    print(f"[信息] 试抓取第一个账号 @{user}")
    raw, err = await fetcher.fetch_tweets(config, username=user)
    if err:
        print(f"[失败] 抓取异常: {err}")
        return
    print(f"[成功] 抓取到 {len(raw)} 条")
    uid = None
    for t in raw:
        uid = t.get("core", {}).get("user_results", {}).get("result", {}).get("rest_id")
        if uid:
            break
    passed = 0
    for t in raw:
        ok, result, data = tweet_filter.filter_tweet(t, uid or "", strict_promo=True)
        flag = "通过" if ok else f"丢弃({result})"
        print(f"  {t.get('rest_id')}: {flag} | {(data.get('content_raw') or '')[:50]}")
        if ok:
            passed += 1
    print(f"[汇总] {passed}/{len(raw)} 条通过过滤（含引流/非行情过滤）")


def main():
    load_dotenv()
    config = load_config()
    ensure_dirs()
    validate_env()
    setup_logging(config)
    enforce_cookie_permissions(config)
    db.init_db()

    if "--test" in sys.argv:
        asyncio.run(test_mode(config))
        return

    logger.info("x-multi-sync 服务正在启动")
    notifier.send_alert(
        f"服务已启动：{datetime.now(UTC8).strftime('%Y-%m-%d %H:%M:%S')}",
        "INFO",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai", event_loop=loop)

    interval = config["scheduler"]["interval_minutes"]
    jitter = config["scheduler"]["jitter_minutes"] * 60
    scheduler.add_job(
        run_sync_cycle, "interval", minutes=interval, jitter=jitter,
        args=[config], id="sync_cycle", name="同步周期", max_instances=1,
    )
    scheduler.add_job(
        daily_digest, "cron",
        hour=config["scheduler"]["daily_digest_hour_utc8"], minute=0,
        id="daily_digest", name="每日报告",
    )
    scheduler.add_job(
        midnight_reset, "cron",
        hour=config["scheduler"]["midnight_reset_hour_utc8"], minute=0,
        id="midnight_reset", name="午夜重置",
    )

    def shutdown(signum, frame):
        logger.info("收到停止信号，正在退出…")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    scheduler.start()
    logger.info(
        f"调度器已启动：每 {interval} 分钟一轮"
        f"（抖动 ±{config['scheduler']['jitter_minutes']} 分钟）"
    )
    loop.run_until_complete(run_sync_cycle(config))
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("服务已停止")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
