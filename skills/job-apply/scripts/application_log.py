#!/usr/bin/env python3
"""Local Markdown application journal. No browser, network, or credentials."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import unquote, urlsplit

SITES = {"hh.ru": "hh", "hh.kz": "hh", "enbek.kz": "enbek"}
PREFIX = "<!-- cv-event: "
SUFFIX = " -->"
BLOCKING = {"prepared", "unknown", "submitted", "already_applied"}
OCCUPIED = {"prepared", "unknown", "submitted"}
STATUSES = BLOCKING | {"skipped", "failed"}


class JournalError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise JournalError("Expected an ISO 8601 timestamp with timezone.") from exc
    if result.tzinfo is None:
        raise JournalError("Timestamp must include a timezone.")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds")


def text_field(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JournalError(f"Missing nonempty string: {key}")
    return value.strip()


def get_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise JournalError("Unknown timezone; install tzdata if OS has no IANA database.") from exc


def load_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise JournalError("Expected a JSON object.")
    return value


def fingerprint(config):
    parts = [SITES[config["site"]], config["account_id"], config["resume_id"]]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def validate_config(config):
    for key in ("site", "account_id", "resume_id", "resume_title", "timezone", "cover_language"):
        text_field(config, key)
    if config["site"] not in SITES:
        raise JournalError("Supported sites: hh.ru, hh.kz, enbek.kz.")
    langs = config.get("languages")
    if not isinstance(langs, list) or not langs or any(x not in ("ru", "kk", "en") for x in langs):
        raise JournalError("Select one or more languages from ru, kk, en.")
    if config["cover_language"] not in langs:
        raise JournalError("Cover language must be one of the selected languages.")
    limit = config.get("max_applications", "missing")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise JournalError("max_applications must be a positive integer or null for all.")
    if type(config.get("allow_without_cover")) is not bool:
        raise JournalError("allow_without_cover must be explicitly true or false.")
    if not isinstance(config.get("filters"), dict) or not config["filters"]:
        raise JournalError("Record the observed and selected filters first.")
    get_zone(config["timezone"])


def canonical_vacancy(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        host = host.removeprefix("www.")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        if host not in SITES:
            raise ValueError()
        match = re.search(r"(?:^|/)vacancy/([A-Za-z0-9_-]+)(?:/|$)", unquote(parsed.path))
        if not match or (SITES[host] == "hh" and not match[1].isdigit()):
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise JournalError("Expected an HTTPS vacancy URL on hh.ru, hh.kz, or enbek.kz.") from exc
    vacancy_id = match[1]
    family = SITES[host]
    path = f"/vacancy/{vacancy_id}" if family == "hh" else f"/ru/vacancy/{vacancy_id}"
    return {"site": host, "vacancy_key": f"{family}:{vacancy_id}", "url": f"https://{host}{path}"}


def slug(value):
    result = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-_").lower()
    return (result[:64].strip(".-_") or "resume")


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise JournalError("Journal output must remain inside the workspace.")
    return path


@contextmanager
def locked(root):
    folder = safe_path(root, "private")
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".application-log.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise JournalError("Journal is locked. Check the other process before removing a stale lock.") from exc
    try:
        yield
    finally:
        lock.rmdir()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def events(root):
    result = []
    folder = safe_path(root, "logs")
    if folder.exists():
        for path in sorted(folder.glob("*.md")):
            if not path.resolve().is_relative_to(root):
                raise JournalError("A journal symlink points outside the workspace.")
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.startswith(PREFIX):
                    continue
                if not line.endswith(SUFFIX):
                    raise JournalError(f"Incomplete journal entry: {path.name}")
                try:
                    item = json.loads(line[len(PREFIX):-len(SUFFIX)])
                    if (item["status"] not in STATUSES or item["schema"] != 1
                            or type(item["sequence"]) is not int or item["sequence"] < 0
                            or not all(isinstance(item[k], str) and item[k] for k in
                                       ("application_id", "identity", "vacancy_key", "run_id", "at"))):
                        raise ValueError()
                    timestamp(item["at"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise JournalError(f"Invalid journal entry: {path.name}") from exc
                result.append(item)
    if len({item["sequence"] for item in result}) != len(result):
        raise JournalError("Conflicting journal sequences; reconcile the local history before sending.")
    return sorted(result, key=lambda x: x["sequence"])


def latest_events(root):
    latest = {}
    for item in events(root):
        latest[item["application_id"]] = item
    return latest


def load_run(root, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise JournalError("Invalid run ID.")
    run = load_json(safe_path(root, f"private/runs/{run_id}.json"))
    validate_config(run["config"])
    return run


def summary(root, run):
    own = [e for e in latest_events(root).values() if e["run_id"] == run["run_id"]]
    occupied = sum(e["status"] in OCCUPIED for e in own)
    counts = {s: sum(e["status"] == s for e in own) for s in sorted(STATUSES)}
    remaining = max(0, (timestamp(run["deadline_at"]) - now()).total_seconds())
    limit = run["config"]["max_applications"]
    reason = None
    if run["status"] != "active":
        reason = "run_finished"
    elif remaining <= 0:
        reason = "one_hour_deadline"
    elif limit is not None and occupied >= limit:
        reason = "application_limit"
    return {"run_id": run["run_id"], "can_prepare": reason is None, "reason": reason,
            "remaining_seconds": int(remaining), "occupied_slots": occupied,
            "counts": counts, "deadline_at": run["deadline_at"],
            "finish_reason": run.get("finish_reason"),
            "log_path": str(safe_path(root, run["log_file"]))}


def eligibility(root, run, vacancy):
    for item in latest_events(root).values():
        if (item["identity"] == run["identity"] and item["vacancy_key"] == vacancy["vacancy_key"]
                and item["status"] in BLOCKING):
            return {"eligible": False, "reason": item["status"],
                    "application_id": item["application_id"]}
    state = summary(root, run)
    return {"eligible": state["can_prepare"], "reason": state["reason"]}


def md(value):
    value = html.escape(str(value)).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def append_event(root, run, event, data):
    path = safe_path(root, run["log_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    machine = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    machine = machine.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    local_at = timestamp(event["at"]).astimezone(get_zone(run["config"]["timezone"]))
    content = ""
    if not path.exists():
        content += (f"# Отклики: {md(run['config']['site'])} / {md(run['config']['resume_title'])}\n\n"
                    "Записи содержат персональные данные. Не публиковать.\n\n")
    content += (f"## {iso(local_at)} — {md(event['status'])}\n\n"
                f"- Вакансия: {md(data.get('vacancy_title', ''))}\n"
                f"- Компания: {md(data.get('company', ''))}\n"
                f"- Ссылка: {md(event['url'])}\n"
                f"- Резюме: {md(run['config']['resume_title'])}\n"
                f"- ID операции: {event['application_id']}\n"
                f"- Письмо: {md(data.get('cover_status', 'not_applicable'))}\n"
                f"- Причина: {md(data.get('reason', ''))}\n"
                f"- Подтверждение: {md(data.get('evidence', ''))}\n\n")
    if data.get("cover_letter"):
        content += "Сопроводительное письмо (данные):\n\n"
        longest = max((len(x) for x in re.findall(r"`+", data["cover_letter"])), default=0)
        fence = "`" * max(3, longest + 1)
        content += fence + "text\n" + data["cover_letter"] + "\n" + fence + "\n\n"
    content += PREFIX + machine + SUFFIX + "\n\n"
    with path.open("a", encoding="utf-8") as stream:
        if os.name != "nt":
            os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def start(root, config, started_at):
    validate_config(config)
    started = timestamp(started_at)
    if started > now():
        raise JournalError("The task start cannot be in the future.")
    if now() >= started + timedelta(hours=1):
        raise JournalError("The one-hour deadline has already passed.")
    identity = fingerprint(config)
    directory = safe_path(root, "private/runs")
    if directory.exists():
        for path in directory.glob("*.json"):
            other = load_json(path)
            if (other["identity"] == identity and other["status"] == "active"
                    and timestamp(other["deadline_at"]) > now()):
                raise JournalError("An active run exists for this resume. Reuse its run ID or finish it first.")
    run_id = uuid.uuid4().hex
    run = {"schema": 1, "run_id": run_id, "identity": identity, "status": "active",
           "started_at": iso(started), "deadline_at": iso(started + timedelta(hours=1)),
           "config": config, "log_file": f"logs/{config['site']}__{slug(config['resume_title'])}-{identity[:10]}.md"}
    write_json(directory / (run_id + ".json"), run)
    return summary(root, run)


def record(root, run, status, data, application_id=None):
    vacancy = canonical_vacancy(text_field(data, "url"))
    if vacancy["site"] != run["config"]["site"]:
        raise JournalError("The vacancy domain differs from the selected site.")
    text_field(data, "vacancy_title")
    text_field(data, "company")
    previous = latest_events(root).get(application_id) if application_id else None
    if application_id and (not previous or previous["run_id"] != run["run_id"]
                           or previous["vacancy_key"] != vacancy["vacancy_key"]):
        raise JournalError("The operation does not belong to this run/vacancy.")
    if status == "prepared":
        if application_id:
            raise JournalError("A new preparation must not reuse an operation ID.")
        gate = eligibility(root, run, vacancy)
        if not gate["eligible"]:
            raise JournalError("Preparation blocked: " + str(gate["reason"]))
        cover_status = data.get("cover_status")
        if cover_status == "included":
            text_field(data, "cover_letter")
        elif cover_status == "unavailable" and run["config"]["allow_without_cover"]:
            text_field(data, "cover_letter")
        else:
            raise JournalError("A cover letter is required unless its absence was explicitly allowed.")
        if data.get("cover_language") != run["config"]["cover_language"]:
            raise JournalError("Record the selected cover language.")
    elif status in ("submitted", "unknown"):
        if not previous or previous["status"] not in ("prepared", "unknown"):
            raise JournalError("This outcome requires a prepared or unresolved operation.")
        text_field(data, "evidence")
    elif status == "already_applied":
        text_field(data, "evidence")
        if previous and previous["status"] not in ("prepared", "unknown"):
            raise JournalError("A terminal operation cannot be overwritten.")
    else:
        text_field(data, "reason")
        if previous:
            if status != "failed" or previous["status"] not in ("prepared", "unknown"):
                raise JournalError("Only a confirmed non-submission can resolve a pending operation as failed.")
            text_field(data, "evidence")
    if previous and status in ("submitted", "unknown", "already_applied", "failed"):
        # The original prepared text is retained in Markdown; do not silently replace it.
        if data.get("cover_letter") and data["cover_letter"] != previous["cover_letter"]:
            raise JournalError("Cover text changed after preparation; record the exact final text before sending.")
        data = {**data, "cover_letter": previous["cover_letter"],
                "cover_status": previous["cover_status"]}
    sequence = max((e["sequence"] for e in events(root)), default=-1) + 1
    event = {"schema": 1, "sequence": sequence, "at": iso(now()),
             "run_id": run["run_id"], "application_id": application_id or uuid.uuid4().hex,
             "identity": run["identity"], "vacancy_key": vacancy["vacancy_key"],
             "url": vacancy["url"], "status": status,
             "cover_letter": data.get("cover_letter", ""),
             "cover_status": data.get("cover_status", "not_applicable")}
    append_event(root, run, event, data)
    return {"application_id": event["application_id"], "status": status, **summary(root, run)}


def finish(root, run, reason):
    if run["status"] == "finished":
        return summary(root, run)
    result = summary(root, run)
    path = safe_path(root, run["log_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        if os.name != "nt":
            os.chmod(path, 0o600)
        stream.write(f"\n## Завершение серии {run['run_id']}\n\n"
                     f"- Время: {iso(now().astimezone(get_zone(run['config']['timezone'])))}\n"
                     f"- Причина: {md(reason)}\n"
                     f"- Статусы: {json.dumps(result['counts'], ensure_ascii=False)}\n\n")
        stream.flush()
        os.fsync(stream.fileno())
    run.update(status="finished", finished_at=iso(now()), finish_reason=reason)
    write_json(safe_path(root, f"private/runs/{run['run_id']}.json"), run)
    return summary(root, run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("start")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--started-at", required=True)
    for name in ("check", "status", "record", "finish"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        if name == "check":
            p.add_argument("--url", required=True)
        if name == "record":
            p.add_argument("--status", choices=sorted(STATUSES), required=True)
            p.add_argument("--data", type=Path, required=True)
            p.add_argument("--application-id")
        if name == "finish":
            p.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = args.workspace.resolve()
    try:
        with locked(root):
            if args.command == "start":
                result = start(root, load_json(args.config), args.started_at)
            else:
                run = load_run(root, args.run)
                if args.command == "status":
                    result = summary(root, run)
                elif args.command == "check":
                    result = eligibility(root, run, canonical_vacancy(args.url))
                elif args.command == "record":
                    result = record(root, run, args.status, load_json(args.data), args.application_id)
                else:
                    result = finish(root, run, args.reason)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if args.command == "check" and not result["eligible"] else 0
    except (JournalError, OSError, ValueError, KeyError) as exc:
        # No input payloads or cookie contents are printed on failure.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
