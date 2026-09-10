#!/usr/bin/env python3
"""按群、北京时间日期和图片备注精确下载 Telegram 图片。"""

from __future__ import annotations

import csv
import getpass
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


CN_TZ = timezone(timedelta(hours=8))


def normalized(text: str) -> str:
    """统一全角/半角和多余空白，但不做模糊匹配。"""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def safe_folder_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return name[:100] or "未命名"


def ask_path(prompt: str) -> Path:
    return Path(input(prompt).strip().strip('"')).expanduser().resolve()


def self_test() -> None:
    assert normalized("  陌生人　大小姐\n") == "陌生人 大小姐"
    assert safe_folder_name('A/B:*?') == "A_B___"
    print("自检通过")


def main() -> int:
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print("缺少 Telethon。请先运行：py -3 -m pip install --upgrade telethon cryptg")
        return 2

    print("只会下载：指定群 + 指定北京时间日期 + 备注完全匹配的图片。")

    notes_file = ask_path("备注名单文件路径（每行一个备注）：")
    if not notes_file.is_file():
        print(f"找不到备注名单：{notes_file}")
        return 2

    notes: dict[str, str] = {}
    for line in notes_file.read_text(encoding="utf-8-sig").splitlines():
        original = line.strip()
        if original:
            notes.setdefault(normalized(original), original)
    if not notes:
        print("备注名单是空的。")
        return 2

    try:
        target_day = datetime.strptime(input("日期（例如 2026-08-29）：").strip(), "%Y-%m-%d").date()
    except ValueError:
        print("日期格式不正确，应为 YYYY-MM-DD。")
        return 2

    chat_ref = input("群用户名、群链接或完整群名：").strip()
    if not chat_ref:
        print("群不能为空。")
        return 2

    output_root = ask_path("保存目录：")
    output_root.mkdir(parents=True, exist_ok=True)

    api_id_text = os.environ.get("TG_API_ID") or input("Telegram api_id：").strip()
    api_hash = os.environ.get("TG_API_HASH") or getpass.getpass("Telegram api_hash（输入时不显示）：").strip()
    if not api_id_text.isdigit() or not api_hash:
        print("api_id 或 api_hash 不正确。")
        return 2
    api_id = int(api_id_text)

    start_local = datetime.combine(target_day, datetime.min.time(), CN_TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    session_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TelegramCaptionDownloader"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_base = session_dir / "account"

    print("首次运行会要求输入手机号、Telegram 验证码，若启用两步验证还会要求密码。")
    print("正在扫描消息文字；此阶段不会下载图片……")

    with TelegramClient(str(session_base), api_id, api_hash) as client:
        try:
            entity = client.get_entity(chat_ref)
        except (ValueError, TypeError):
            wanted = normalized(chat_ref).casefold()
            matches = [d.entity for d in client.iter_dialogs() if normalized(d.name or "").casefold() == wanted]
            if len(matches) != 1:
                print("无法唯一找到该群。请改用群的 @用户名或 t.me 链接。")
                return 2
            entity = matches[0]

        media_messages = []
        scanned = 0
        for message in client.iter_messages(entity, offset_date=end_utc):
            if message.date < start_utc:
                break
            if message.date >= end_utc:
                continue
            scanned += 1
            mime = (message.file.mime_type if message.file else "") or ""
            if message.photo or mime.startswith("image/"):
                media_messages.append(message)

        album_labels: dict[int, set[str]] = defaultdict(set)
        direct_labels: dict[int, set[str]] = defaultdict(set)
        for message in media_messages:
            label = notes.get(normalized(message.raw_text or ""))
            if label:
                direct_labels[message.id].add(label)
                if message.grouped_id:
                    album_labels[message.grouped_id].add(label)

        counts: Counter[str] = Counter()
        rows = []
        for message in media_messages:
            labels = set(direct_labels.get(message.id, ()))
            if message.grouped_id:
                labels.update(album_labels.get(message.grouped_id, ()))
            if not labels:
                continue

            ext = message.file.ext if message.file else ".jpg"
            if not ext or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
                ext = ".jpg"
            stamp = message.date.astimezone(CN_TZ).strftime("%Y%m%d_%H%M%S")
            filename = f"{stamp}_{message.id}{ext.lower()}"

            for label in sorted(labels):
                folder = output_root / safe_folder_name(label)
                folder.mkdir(parents=True, exist_ok=True)
                destination = folder / filename
                if not destination.exists():
                    downloaded = message.download_media(file=str(destination))
                    if not downloaded:
                        print(f"下载失败：消息 {message.id}")
                        continue
                counts[label] += 1
                rows.append([label, message.id, message.date.astimezone(CN_TZ).isoformat(), str(destination)])
                print(f"已提取：{label} -> {filename}")

    report = output_root / "提取报告.csv"
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["备注", "消息ID", "北京时间", "本地文件"])
        writer.writerows(rows)

    unmatched = [label for label in notes.values() if counts[label] == 0]
    (output_root / "未匹配备注.txt").write_text("\n".join(unmatched), encoding="utf-8-sig")
    print(f"完成：扫描 {scanned} 条消息，下载/确认 {sum(counts.values())} 张，未匹配 {len(unmatched)} 条。")
    print(f"报告：{report}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        raise SystemExit(main())
