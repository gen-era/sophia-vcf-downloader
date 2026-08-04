#!/usr/bin/env python3
"""Download SOPHiA full_variant_table.vcf files and sample metadata.

Single-client usage:

    python3 download_sophia_vcfss.py --client-folder 203441

Multi-client usage:

    python3 download_sophia_vcfss.py --accounts-xlsx ./sophia_accounts.xlsx --workers 4

Resume usage:

    python3 download_sophia_vcfss.py --accounts-xlsx ./sophia_accounts.xlsx --resume-from-latest-manifest

The script expects sg-upload-v2-wrapper.py to be present in the current
directory and the current SOPHiA CLI session to be able to login via
login-iam --client-id.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Semaphore
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


VCF_FILENAME = "full_variant_table.vcf"
SAMPLE_METADATA_FILENAME = "sample_metadata.xlsx"
MANIFEST_COLUMNS = [
    "run_id",
    "file_id",
    "userRef",
    "analysisId",
    "output_path",
    "status",
    "metadata_status",
    "metadata_path",
    "metadata_error",
    "error",
]
RUN_IDS_COLUMNS = ["run_id", "status"]
ACCOUNTS_MANIFEST_COLUMNS = [
    "client_id",
    "institution_name",
    "account_status",
    "run_count",
    "processed_run_count",
    "downloaded_count",
    "metadata_written_count",
    "error",
]
COMPLETED_DOWNLOAD_STATUSES = {"downloaded", "skipped_exists", "no_vcf", "skipped_existing_run"}
COMMAND_TIMEOUT_SECONDS: int | None = None
DOWNLOAD_TIMEOUT_SECONDS: int | None = None
DOWNLOAD_START_TIMEOUT_SECONDS: int | None = None
DOWNLOAD_STALL_TIMEOUT_SECONDS: int | None = None
TURKISH_TRANSLATION = str.maketrans(
    {
        "\u0130": "I",
        "I": "I",
        "\u0131": "i",
        "\u015e": "S",
        "\u015f": "s",
        "\u00d6": "O",
        "\u00f6": "o",
        "\u00dc": "U",
        "\u00fc": "u",
        "\u011e": "G",
        "\u011f": "g",
        "\u00c7": "C",
        "\u00e7": "c",
    }
)
NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str


@dataclass(frozen=True)
class Account:
    client_id: str
    institution_name: str


@dataclass(frozen=True)
class ManifestRow:
    run_id: str
    file_id: str = ""
    user_ref: str = ""
    analysis_id: str = ""
    output_path: str = ""
    status: str = ""
    metadata_status: str = ""
    metadata_path: str = ""
    metadata_error: str = ""
    error: str = ""

    def as_tsv_row(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "file_id": self.file_id,
            "userRef": self.user_ref,
            "analysisId": self.analysis_id,
            "output_path": self.output_path,
            "status": self.status,
            "metadata_status": self.metadata_status,
            "metadata_path": self.metadata_path,
            "metadata_error": self.metadata_error,
            "error": self.error,
        }


@dataclass(frozen=True)
class AccountManifestRow:
    client_id: str
    institution_name: str
    account_status: str
    run_count: int = 0
    processed_run_count: int = 0
    downloaded_count: int = 0
    metadata_written_count: int = 0
    error: str = ""

    def as_tsv_row(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "institution_name": self.institution_name,
            "account_status": self.account_status,
            "run_count": str(self.run_count),
            "processed_run_count": str(self.processed_run_count),
            "downloaded_count": str(self.downloaded_count),
            "metadata_written_count": str(self.metadata_written_count),
            "error": self.error,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download full_variant_table.vcf files and sample metadata from SOPHiA runs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--wrapper", default="./sg-upload-v2-wrapper.py", help="Path to SOPHiA wrapper script.")
    parser.add_argument("--python", default="python3", help="Python executable used to run the wrapper.")
    parser.add_argument("--client-folder", default="203441", help="Single-client output folder.")
    parser.add_argument("--accounts-xlsx", help="Workbook containing client IDs, institution names, and checked marks.")
    parser.add_argument("--output-root", default=".", help="Root folder for account output folders.")
    parser.add_argument("--client-id-col", type=int, default=1, help="1-based Excel column for client IDs.")
    parser.add_argument("--name-col", type=int, default=2, help="1-based Excel column for institution names.")
    parser.add_argument("--checked-col", type=int, default=4, help="1-based Excel column containing 'checked'.")
    parser.add_argument("--limit", type=int, default=10000, help="Number of recent runs requested from status.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers for per-run processing.")
    parser.add_argument("--max-accounts", type=int, help="Process only the first N checked accounts.")
    parser.add_argument("--dry-run", action="store_true", help="List runs/files and write TSV/XLSX metadata, but skip VCF downloads.")
    parser.add_argument("--force", action="store_true", help="Re-download VCF files even if targets exist and are non-empty.")
    parser.add_argument("--force-metadata", action="store_true", help="Regenerate sample_metadata.xlsx even if it exists.")
    parser.add_argument(
        "--resume-from-latest-manifest",
        action="store_true",
        help="In multi-client mode, start from the account with the newest download_manifest.tsv and skip earlier accounts.",
    )
    parser.add_argument(
        "--resume-verify-last-runs",
        type=int,
        default=10,
        help="When resuming, re-verify this many latest manifest run IDs in the active account.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not skip runs already completed in the manifest or existing output folders.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print per-run sample/list/download messages.")
    parser.add_argument("--command-timeout", type=int, default=300, help="Timeout in seconds for login/status/sample/list commands.")
    parser.add_argument("--download-timeout", type=int, default=2700, help="Timeout in seconds for each VCF download command.")
    parser.add_argument(
        "--download-start-timeout",
        type=int,
        default=180,
        help="Abort a VCF download if the .part file is not created within this many seconds.",
    )
    parser.add_argument(
        "--download-stall-timeout",
        type=int,
        default=300,
        help="Abort a VCF download if the .part file size does not grow for this many seconds.",
    )
    return parser.parse_args()


def run_wrapper(
    python_exe: str,
    wrapper: str,
    command_args: list[str],
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if timeout_seconds is None:
        timeout_seconds = COMMAND_TIMEOUT_SECONDS
    command = [python_exe, wrapper, *command_args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr = f"{stderr}\nTimed out after {timeout_seconds} seconds: {' '.join(command)}".strip()
        return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)


def compact_error(stdout: str, stderr: str, returncode: int | None = None, max_len: int = 500) -> str:
    chunks: list[str] = []
    if returncode is not None:
        chunks.append(f"returncode={returncode}")
    if stderr.strip():
        chunks.append(f"stderr={stderr.strip()}")
    if stdout.strip():
        chunks.append(f"stdout={stdout.strip()}")
    message = " | ".join(chunks)
    return message[:max_len]


def parse_status_output(stdout: str) -> list[RunStatus]:
    runs: list[RunStatus] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        run_id, status = match.group(1), match.group(2)
        if run_id in seen:
            continue
        seen.add(run_id)
        runs.append(RunStatus(run_id=run_id, status=status))
    return runs


def load_json_from_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def get_recent_runs(python_exe: str, wrapper: str, limit: int) -> list[RunStatus]:
    result = run_wrapper(python_exe, wrapper, ["status", "--limit", str(limit)])
    if result.returncode != 0:
        raise RuntimeError(compact_error(result.stdout, result.stderr, result.returncode, max_len=2000))
    runs = parse_status_output(result.stdout)
    if not runs:
        raise RuntimeError(f"No run IDs found in status output: {result.stdout[:1000]}")
    return runs


def login_client(python_exe: str, wrapper: str, client_id: str) -> None:
    result = run_wrapper(python_exe, wrapper, ["login-iam", "--client-id", client_id])
    if result.returncode != 0:
        raise RuntimeError(compact_error(result.stdout, result.stderr, result.returncode, max_len=2000))


def write_run_ids(client_dir: Path, runs: Iterable[RunStatus]) -> None:
    path = client_dir / "run_ids.tsv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_IDS_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for run in runs:
            writer.writerow({"run_id": run.run_id, "status": run.status})


def read_tsv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def ensure_tsv_header(path: Path, fieldnames: list[str]) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    rows = read_tsv_rows(path)
    with path.open("r", newline="", encoding="utf-8") as handle:
        first_line = handle.readline().rstrip("\n")
    existing = first_line.split("\t") if first_line else []
    if existing == fieldnames:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def append_manifest_rows(client_dir: Path, rows: Iterable[ManifestRow]) -> None:
    path = client_dir / "download_manifest.tsv"
    ensure_tsv_header(path, MANIFEST_COLUMNS)
    file_exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, delimiter="\t", lineterminator="\n")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row.as_tsv_row())


def append_account_manifest_row(output_root: Path, row: AccountManifestRow) -> None:
    path = output_root / "accounts_manifest.tsv"
    ensure_tsv_header(path, ACCOUNTS_MANIFEST_COLUMNS)
    file_exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACCOUNTS_MANIFEST_COLUMNS, delimiter="\t", lineterminator="\n")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row.as_tsv_row())


def load_completed_run_ids_from_manifest(client_dir: Path) -> set[str]:
    path = client_dir / "download_manifest.tsv"
    rows = read_tsv_rows(path)
    rows_by_run: dict[str, list[str]] = {}
    for row in rows:
        run_id = row.get("run_id", "")
        status = row.get("status", "")
        if run_id:
            rows_by_run.setdefault(run_id, []).append(status)

    completed: set[str] = set()
    for run_id, statuses in rows_by_run.items():
        if statuses and all(status in COMPLETED_DOWNLOAD_STATUSES for status in statuses):
            completed.add(run_id)
    return completed


def load_manifest_statuses_by_run(client_dir: Path) -> dict[str, list[str]]:
    rows = read_tsv_rows(client_dir / "download_manifest.tsv")
    statuses_by_run: dict[str, list[str]] = {}
    for row in rows:
        run_id = row.get("run_id", "")
        status = row.get("status", "")
        if run_id:
            statuses_by_run.setdefault(run_id, []).append(status)
    return statuses_by_run


def load_manifest_run_order(client_dir: Path) -> list[str]:
    rows = read_tsv_rows(client_dir / "download_manifest.tsv")
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        run_id = row.get("run_id", "")
        if run_id and run_id not in seen:
            ordered.append(run_id)
            seen.add(run_id)
    return ordered


def completed_manifest_run_ids(client_dir: Path) -> set[str]:
    completed: set[str] = set()
    for run_id, statuses in load_manifest_statuses_by_run(client_dir).items():
        if statuses and all(status in COMPLETED_DOWNLOAD_STATUSES for status in statuses):
            completed.add(run_id)
    return completed


def failed_manifest_run_ids(client_dir: Path) -> set[str]:
    failed: set[str] = set()
    for run_id, statuses in load_manifest_statuses_by_run(client_dir).items():
        if any(status not in COMPLETED_DOWNLOAD_STATUSES for status in statuses):
            failed.add(run_id)
    return failed


def last_manifest_run_ids(client_dir: Path, count: int) -> set[str]:
    if count <= 0:
        return set()
    return set(load_manifest_run_order(client_dir)[-count:])


def load_run_ids_with_existing_vcfs(client_dir: Path) -> set[str]:
    completed: set[str] = set()
    if not client_dir.exists():
        return completed

    for run_dir in client_dir.iterdir():
        if not run_dir.is_dir() or not run_dir.name.isdigit():
            continue
        for vcf_path in run_dir.glob("*_full_variant_table*.vcf"):
            if vcf_path.is_file() and vcf_path.stat().st_size > 0:
                completed.add(run_dir.name)
                break
    return completed


def load_run_ids_with_metadata(client_dir: Path) -> set[str]:
    completed: set[str] = set()
    if not client_dir.exists():
        return completed
    for run_dir in client_dir.iterdir():
        metadata_path = run_dir / SAMPLE_METADATA_FILENAME
        if run_dir.is_dir() and run_dir.name.isdigit() and metadata_path.exists() and metadata_path.stat().st_size > 0:
            completed.add(run_dir.name)
    return completed


def select_full_variant_vcfs(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        raise ValueError(f"file --list returned {type(files).__name__}, expected list")
    selected: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        name = item.get("name")
        if filename == VCF_FILENAME or name == VCF_FILENAME:
            selected.append(item)
    return selected


def safe_filename_part(value: str) -> str:
    safe = value.translate(TURKISH_TRANSLATION)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", safe.strip())
    safe = re.sub(r"\s+", " ", safe)
    safe = safe.strip(" ._")
    return safe or "unknown"


def legacy_safe_filename_part(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", " ", safe)
    safe = safe.strip(" ._")
    return safe or "unknown"


def windows_to_wsl_path(path_text: str) -> str:
    match = re.match(r"^([A-Za-z]):\\(.*)$", path_text)
    if not match:
        return path_text
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def cell_ref_to_indexes(cell_ref: str) -> tuple[int, int]:
    match = re.match(r"^([A-Z]+)(\d+)$", cell_ref)
    if not match:
        raise ValueError(f"Invalid cell reference: {cell_ref}")
    col_letters, row_text = match.groups()
    col_index = 0
    for char in col_letters:
        col_index = col_index * 26 + (ord(char) - ord("A") + 1)
    return int(row_text), col_index


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: list[str] = []
    for si in root.findall("main:si", NS):
        texts = [node.text or "" for node in si.findall(".//main:t", NS)]
        strings.append("".join(texts))
    return strings


def first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    first_sheet = workbook.find("main:sheets/main:sheet", NS)
    if first_sheet is None:
        raise ValueError("Workbook has no sheets")
    rel_id = first_sheet.attrib[f"{{{NS['rel']}}}id"]

    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("pkgrel:Relationship", NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"]
            if target.startswith("/"):
                return target.lstrip("/")
            if target.startswith("xl/"):
                return target
            return "xl/" + target
    raise ValueError(f"Could not resolve first sheet relationship: {rel_id}")


def parse_cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        texts = [node.text or "" for node in cell.findall(".//main:t", NS)]
        return "".join(texts)

    value_node = cell.find("main:v", NS)
    if value_node is None or value_node.text is None:
        return None

    raw = value_node.text
    if cell_type == "s":
        return shared_strings[int(raw)]
    if cell_type in {"str", "b"}:
        return raw
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def read_xlsx_first_sheet_rows(path: Path) -> list[list[Any]]:
    with zipfile.ZipFile(path) as zf:
        shared_strings = load_shared_strings(zf)
        sheet_path = first_sheet_path(zf)
        sheet = ET.fromstring(zf.read(sheet_path))

    rows: list[list[Any]] = []
    for row_node in sheet.findall(".//main:sheetData/main:row", NS):
        values_by_col: dict[int, Any] = {}
        max_col = 0
        for cell in row_node.findall("main:c", NS):
            ref = cell.attrib.get("r")
            if not ref:
                continue
            _, col_index = cell_ref_to_indexes(ref)
            values_by_col[col_index] = parse_cell_value(cell, shared_strings)
            max_col = max(max_col, col_index)
        if max_col:
            rows.append([values_by_col.get(col) for col in range(1, max_col + 1)])
    return rows


def load_accounts_from_xlsx(
    path_text: str,
    client_id_col: int,
    name_col: int,
    checked_col: int,
    max_accounts: int | None,
) -> list[Account]:
    raw_path = Path(path_text)
    path = raw_path if raw_path.exists() else Path(windows_to_wsl_path(path_text))
    if not path.exists():
        raise FileNotFoundError(f"Accounts workbook not found: {path}")
    rows = read_xlsx_first_sheet_rows(path)
    accounts: list[Account] = []
    for row in rows:
        max_needed = max(client_id_col, name_col, checked_col)
        if len(row) < max_needed:
            continue
        checked = str(row[checked_col - 1] or "").strip().lower()
        if checked != "checked":
            continue
        raw_client_id = row[client_id_col - 1]
        raw_name = row[name_col - 1]
        if raw_client_id in (None, "") or raw_name in (None, ""):
            continue
        client_id = str(raw_client_id).strip()
        if client_id.endswith(".0"):
            client_id = client_id[:-2]
        accounts.append(Account(client_id=client_id, institution_name=str(raw_name).strip()))
        if max_accounts is not None and len(accounts) >= max_accounts:
            break
    return accounts


def process_date_to_human(process_date: Any) -> str:
    if process_date in (None, ""):
        return ""
    try:
        millis = int(float(str(process_date)))
    except ValueError:
        return ""
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%d-%m-%Y")


def xlsx_cell_xml(row_index: int, col_index: int, value: Any) -> str:
    col_letters = ""
    remaining = col_index
    while remaining:
        remaining, remainder = divmod(remaining - 1, 26)
        col_letters = chr(ord("A") + remainder) + col_letters
    ref = f"{col_letters}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def write_simple_xlsx(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    all_rows = [headers, *rows]
    row_xml = []
    for row_index, row in enumerate(all_rows, start=1):
        cells = "".join(xlsx_cell_xml(row_index, col_index, value) for col_index, value in enumerate(row, start=1))
        row_xml.append(f'<row r="{row_index}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sample Metadata" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def write_sample_metadata_xlsx(samples: Any, path: Path) -> int:
    if not isinstance(samples, list):
        raise ValueError(f"sample returned {type(samples).__name__}, expected list")
    rows: list[list[Any]] = []
    for item in samples:
        if not isinstance(item, dict):
            continue
        process_date = item.get("processDate")
        rows.append(
            [
                item.get("userRef", ""),
                item.get("analysisType", ""),
                item.get("genePanel", ""),
                process_date if process_date is not None else "",
                process_date_to_human(process_date),
            ]
        )
    write_simple_xlsx(path, ["userRef", "analysisType", "genePanel", "processDate", "Date"], rows)
    return len(rows)


def write_sample_metadata_for_run(
    run_id: str,
    python_exe: str,
    wrapper: str,
    run_dir: Path,
    force_metadata: bool,
    verbose_prefix: str = "",
) -> tuple[str, str, str]:
    metadata_path = run_dir / SAMPLE_METADATA_FILENAME
    if metadata_path.exists() and metadata_path.stat().st_size > 0 and not force_metadata:
        return "metadata_skipped_exists", str(metadata_path), ""
    if verbose_prefix:
        print(f"{verbose_prefix} run {run_id}: sample metadata")
    result = run_wrapper(python_exe, wrapper, ["sample", "--run-id", run_id])
    if result.returncode != 0:
        return "metadata_failed", str(metadata_path), compact_error(result.stdout, result.stderr, result.returncode)
    try:
        samples = load_json_from_stdout(result.stdout)
        write_sample_metadata_xlsx(samples, metadata_path)
    except Exception as exc:  # noqa: BLE001 - keep per-run failure in manifest.
        return "metadata_failed", str(metadata_path), f"{type(exc).__name__}: {exc}"
    return "metadata_written", str(metadata_path), ""


def planned_output_path(client_dir: Path, run_id: str, file_info: dict[str, Any], used_names: set[str]) -> Path:
    raw_user_ref = str(file_info.get("userRef") or f"unknown_{run_id}")
    user_ref = safe_filename_part(raw_user_ref)
    stem = f"{user_ref}_full_variant_table"
    filename = f"{stem}.vcf"

    if filename in used_names:
        analysis_id = file_info.get("analysisId")
        file_id = file_info.get("id")
        suffix = analysis_id if analysis_id not in (None, "") else file_id
        filename = f"{stem}_{suffix}.vcf"

    while filename in used_names:
        file_id = file_info.get("id", "duplicate")
        filename = f"{stem}_{file_id}_{len(used_names) + 1}.vcf"

    used_names.add(filename)
    return client_dir / run_id / filename


def download_file(
    python_exe: str,
    wrapper: str,
    file_id: str,
    output_path: Path,
) -> subprocess.CompletedProcess[str]:
    command = [python_exe, wrapper, "file", "--download", "--file-id", file_id, "--file-out", str(output_path)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started_at = time.monotonic()
    last_size: int | None = None
    last_growth_at = started_at

    while process.poll() is None:
        now = time.monotonic()
        if DOWNLOAD_TIMEOUT_SECONDS is not None and now - started_at > DOWNLOAD_TIMEOUT_SECONDS:
            process.kill()
            stdout, stderr = process.communicate()
            stderr = f"{stderr}\nTimed out after {DOWNLOAD_TIMEOUT_SECONDS} seconds: {' '.join(command)}".strip()
            return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)

        if output_path.exists():
            current_size = output_path.stat().st_size
            if last_size is None or current_size > last_size:
                last_size = current_size
                last_growth_at = now
            elif DOWNLOAD_STALL_TIMEOUT_SECONDS is not None and now - last_growth_at > DOWNLOAD_STALL_TIMEOUT_SECONDS:
                process.kill()
                stdout, stderr = process.communicate()
                stderr = (
                    f"{stderr}\nDownload stalled for {DOWNLOAD_STALL_TIMEOUT_SECONDS} seconds "
                    f"with {current_size} bytes written: {' '.join(command)}"
                ).strip()
                return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)
        elif DOWNLOAD_START_TIMEOUT_SECONDS is not None and now - started_at > DOWNLOAD_START_TIMEOUT_SECONDS:
            process.kill()
            stdout, stderr = process.communicate()
            stderr = (
                f"{stderr}\nDownload did not create output file within {DOWNLOAD_START_TIMEOUT_SECONDS} seconds: "
                f"{' '.join(command)}"
            ).strip()
            return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)

        time.sleep(5)

    stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)


def expected_content_length(file_info: dict[str, Any]) -> int | None:
    value = file_info.get("contentLength")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def existing_vcf_is_complete(output_path: Path, file_info: dict[str, Any]) -> bool:
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return False
    expected_size = expected_content_length(file_info)
    if expected_size is None:
        return True
    return output_path.stat().st_size == expected_size


def download_file_safely(
    python_exe: str,
    wrapper: str,
    file_id: str,
    output_path: Path,
    file_info: dict[str, Any],
) -> tuple[bool, str]:
    part_path = output_path.with_name(output_path.name + ".part")
    if part_path.exists():
        part_path.unlink()

    result = download_file(python_exe, wrapper, file_id, part_path)
    if result.returncode != 0:
        if part_path.exists():
            part_path.unlink()
        return False, compact_error(result.stdout, result.stderr, result.returncode)

    if not part_path.exists() or part_path.stat().st_size <= 0:
        if part_path.exists():
            part_path.unlink()
        return False, f"Download reported success but temp file is missing or empty: {part_path}"

    expected_size = expected_content_length(file_info)
    if expected_size is not None and part_path.stat().st_size != expected_size:
        part_path.unlink()
        return False, f"Downloaded size mismatch: expected {expected_size}, got {part_path.stat().st_size}"

    part_path.replace(output_path)
    return True, ""


def process_run(
    run: RunStatus,
    python_exe: str,
    wrapper: str,
    client_dir: Path,
    dry_run: bool,
    force: bool,
    force_metadata: bool,
    download_semaphore: Semaphore,
    verbose_prefix: str = "",
) -> list[ManifestRow]:
    run_dir = client_dir / run.run_id
    metadata_status, metadata_path, metadata_error = write_sample_metadata_for_run(
        run.run_id,
        python_exe,
        wrapper,
        run_dir,
        force_metadata,
        verbose_prefix,
    )

    if verbose_prefix:
        print(f"{verbose_prefix} run {run.run_id}: file list")
    list_result = run_wrapper(python_exe, wrapper, ["file", "--list", "--run-id", run.run_id])
    if list_result.returncode != 0:
        return [
            ManifestRow(
                run_id=run.run_id,
                status="list_failed",
                metadata_status=metadata_status,
                metadata_path=metadata_path,
                metadata_error=metadata_error,
                error=compact_error(list_result.stdout, list_result.stderr, list_result.returncode),
            )
        ]

    try:
        files = load_json_from_stdout(list_result.stdout)
        vcfs = select_full_variant_vcfs(files)
    except Exception as exc:  # noqa: BLE001 - manifest should preserve all per-run failures.
        return [
            ManifestRow(
                run_id=run.run_id,
                status="parse_failed",
                metadata_status=metadata_status,
                metadata_path=metadata_path,
                metadata_error=metadata_error,
                error=f"{type(exc).__name__}: {exc}",
            )
        ]

    if not vcfs:
        return [
            ManifestRow(
                run_id=run.run_id,
                status="no_vcf",
                metadata_status=metadata_status,
                metadata_path=metadata_path,
                metadata_error=metadata_error,
            )
        ]

    rows: list[ManifestRow] = []
    used_names: set[str] = set()
    for file_info in vcfs:
        file_id_value = file_info.get("id")
        if file_id_value in (None, ""):
            rows.append(
                ManifestRow(
                    run_id=run.run_id,
                    user_ref=str(file_info.get("userRef") or ""),
                    analysis_id=str(file_info.get("analysisId") or ""),
                    status="missing_file_id",
                    metadata_status=metadata_status,
                    metadata_path=metadata_path,
                    metadata_error=metadata_error,
                    error="Selected VCF record has no id field.",
                )
            )
            continue

        file_id = str(file_id_value)
        output_path = planned_output_path(client_dir, run.run_id, file_info, used_names)
        user_ref = str(file_info.get("userRef") or f"unknown_{run.run_id}")
        analysis_id = str(file_info.get("analysisId") or "")

        if dry_run:
            rows.append(
                ManifestRow(
                    run_id=run.run_id,
                    file_id=file_id,
                    user_ref=user_ref,
                    analysis_id=analysis_id,
                    output_path=str(output_path),
                    status="dry_run",
                    metadata_status=metadata_status,
                    metadata_path=metadata_path,
                    metadata_error=metadata_error,
                )
            )
            continue

        if existing_vcf_is_complete(output_path, file_info) and not force:
            rows.append(
                ManifestRow(
                    run_id=run.run_id,
                    file_id=file_id,
                    user_ref=user_ref,
                    analysis_id=analysis_id,
                    output_path=str(output_path),
                    status="skipped_exists",
                    metadata_status=metadata_status,
                    metadata_path=metadata_path,
                    metadata_error=metadata_error,
                )
            )
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if verbose_prefix:
            print(f"{verbose_prefix} run {run.run_id}: download {output_path.name}")
        with download_semaphore:
            download_ok, download_error = download_file_safely(python_exe, wrapper, file_id, output_path, file_info)
        if download_ok:
            rows.append(
                ManifestRow(
                    run_id=run.run_id,
                    file_id=file_id,
                    user_ref=user_ref,
                    analysis_id=analysis_id,
                    output_path=str(output_path),
                    status="downloaded",
                    metadata_status=metadata_status,
                    metadata_path=metadata_path,
                    metadata_error=metadata_error,
                )
            )
        else:
            rows.append(
                ManifestRow(
                    run_id=run.run_id,
                    file_id=file_id,
                    user_ref=user_ref,
                    analysis_id=analysis_id,
                    output_path=str(output_path),
                    status="download_failed",
                    metadata_status=metadata_status,
                    metadata_path=metadata_path,
                    metadata_error=metadata_error,
                    error=download_error,
                )
            )

    return rows


def account_output_dir(output_root: Path, account: Account, args: argparse.Namespace) -> Path:
    ascii_name = safe_filename_part(account.institution_name)
    legacy_name = legacy_safe_filename_part(account.institution_name)
    ascii_dir = output_root / ascii_name
    legacy_dir = output_root / legacy_name

    if legacy_dir.exists() and legacy_dir != ascii_dir and not ascii_dir.exists():
        legacy_dir.rename(ascii_dir)
        print(f"Renamed account folder: {legacy_name} -> {ascii_name}")

    return ascii_dir


def latest_manifest_path(output_root: Path) -> Path | None:
    manifests = [
        path
        for path in output_root.glob("*/download_manifest.tsv")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not manifests:
        return None
    return max(manifests, key=lambda path: path.stat().st_mtime)


def skip_accounts_before_latest_manifest(accounts: list[Account], output_root: Path, args: argparse.Namespace) -> list[Account]:
    latest_manifest = latest_manifest_path(output_root)
    if latest_manifest is None:
        print("Resume from latest manifest: no download_manifest.tsv found; starting from first account")
        return accounts

    latest_account_dir = latest_manifest.parent.resolve()
    for index, account in enumerate(accounts):
        if account_output_dir(output_root, account, args).resolve() == latest_account_dir:
            print(f"Resume from latest manifest: starting at {account.institution_name} ({latest_manifest})")
            return accounts[index:]

    print(f"Resume from latest manifest: newest manifest was not matched to the workbook accounts ({latest_manifest}); starting from first account")
    return accounts


def choose_runs_to_process(
    client_dir: Path,
    runs: list[RunStatus],
    resume_enabled: bool,
    force_metadata: bool,
    resume_verify_last_runs: int = 10,
) -> tuple[list[RunStatus], list[ManifestRow]]:
    if not resume_enabled:
        return list(runs), []

    metadata_run_ids = load_run_ids_with_metadata(client_dir)
    completed_run_ids = completed_manifest_run_ids(client_dir)
    failed_run_ids = failed_manifest_run_ids(client_dir)
    verify_run_ids = last_manifest_run_ids(client_dir, resume_verify_last_runs)

    work: list[RunStatus] = []
    skipped_rows: list[ManifestRow] = []
    for run in runs:
        has_metadata = run.run_id in metadata_run_ids and not force_metadata
        if run.run_id in completed_run_ids and run.run_id not in failed_run_ids and run.run_id not in verify_run_ids:
            skipped_rows.append(ManifestRow(run_id=run.run_id, status="skipped_existing_run", metadata_status="metadata_skipped_exists" if has_metadata else ""))
            continue
        work.append(run)
    return work, skipped_rows


def summarize_rows(rows: list[ManifestRow]) -> tuple[dict[str, int], int, int]:
    status_counts: dict[str, int] = {}
    downloaded_count = 0
    metadata_written_count = 0
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        if row.status == "downloaded":
            downloaded_count += 1
        if row.metadata_status == "metadata_written":
            metadata_written_count += 1
    return status_counts, downloaded_count, metadata_written_count


def process_run_work_items(
    account_label: str,
    runs: list[RunStatus],
    work_items: list[RunStatus],
    skipped_rows: list[ManifestRow],
    args: argparse.Namespace,
    client_dir: Path,
) -> list[ManifestRow]:
    all_rows: list[ManifestRow] = list(skipped_rows)
    if skipped_rows:
        append_manifest_rows(client_dir, skipped_rows)
    print(f"[{account_label}] Resume: skipping {len(skipped_rows)} runs, processing {len(work_items)} active runs")

    verbose_prefix = f"[{account_label}]" if args.verbose else ""
    download_semaphore = Semaphore(args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_run,
                run,
                args.python,
                args.wrapper,
                client_dir,
                args.dry_run,
                args.force,
                args.force_metadata,
                download_semaphore,
                verbose_prefix,
            ): run
            for run in work_items
        }
        for index, future in enumerate(as_completed(futures), start=1):
            run = futures[future]
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001 - keep processing after unexpected per-run failures.
                rows = [ManifestRow(run_id=run.run_id, status="unexpected_failed", error=f"{type(exc).__name__}: {exc}")]
            all_rows.extend(rows)
            append_manifest_rows(client_dir, rows)
            if index % 25 == 0 or index == len(work_items):
                print(f"[{account_label}] Processed {index}/{len(work_items)} active runs")
    return all_rows


def process_single_client(args: argparse.Namespace) -> int:
    client_dir = Path(args.output_root) / args.client_folder
    client_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching latest {args.limit} runs...")
    try:
        runs = get_recent_runs(args.python, args.wrapper, args.limit)
    except Exception as exc:  # noqa: BLE001 - command line entrypoint should report cleanly.
        print(f"Failed to fetch run statuses: {exc}", file=sys.stderr)
        return 1

    write_run_ids(client_dir, runs)
    print(f"Saved {len(runs)} run IDs to {client_dir / 'run_ids.tsv'}")
    work_items, skipped_rows = choose_runs_to_process(
        client_dir,
        runs,
        resume_enabled=not args.no_resume and not args.force,
        force_metadata=args.force_metadata,
        resume_verify_last_runs=args.resume_verify_last_runs,
    )
    rows = process_run_work_items(args.client_folder, runs, work_items, skipped_rows, args, client_dir)
    status_counts, _, _ = summarize_rows(rows)
    summary = ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    print(f"Saved manifest to {client_dir / 'download_manifest.tsv'}")
    print(f"Done: {summary}")
    return 0


def process_account(
    account: Account,
    account_index: int,
    account_total: int,
    args: argparse.Namespace,
    output_root: Path,
) -> AccountManifestRow:
    client_dir = account_output_dir(output_root, account, args)
    client_dir.mkdir(parents=True, exist_ok=True)
    label = f"{account.institution_name} (client-id {account.client_id})"

    print(f"[{account_index}/{account_total}] {label}: logging in")
    try:
        login_client(args.python, args.wrapper, account.client_id)
    except Exception as exc:  # noqa: BLE001 - continue with next account.
        error = f"{type(exc).__name__}: {exc}"
        print(f"[{account_index}/{account_total}] {label}: login failed")
        return AccountManifestRow(account.client_id, account.institution_name, "login_failed", error=error)

    print(f"[{account_index}/{account_total}] {label}: fetching latest {args.limit} runs")
    try:
        runs = get_recent_runs(args.python, args.wrapper, args.limit)
    except Exception as exc:  # noqa: BLE001 - continue with next account.
        error = f"{type(exc).__name__}: {exc}"
        print(f"[{account_index}/{account_total}] {label}: status failed")
        return AccountManifestRow(account.client_id, account.institution_name, "status_failed", error=error)
    write_run_ids(client_dir, runs)
    print(f"[{account_index}/{account_total}] {label}: fetched {len(runs)} runs")

    work_items, skipped_rows = choose_runs_to_process(
        client_dir,
        runs,
        resume_enabled=not args.no_resume and not args.force,
        force_metadata=args.force_metadata,
        resume_verify_last_runs=args.resume_verify_last_runs,
    )
    rows = process_run_work_items(
        account.institution_name,
        runs,
        work_items,
        skipped_rows,
        args,
        client_dir,
    )
    status_counts, downloaded_count, metadata_written_count = summarize_rows(rows)
    summary = ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    print(f"[{account_index}/{account_total}] {label}: done {summary}")
    return AccountManifestRow(
        client_id=account.client_id,
        institution_name=account.institution_name,
        account_status="completed",
        run_count=len(runs),
        processed_run_count=len(work_items),
        downloaded_count=downloaded_count,
        metadata_written_count=metadata_written_count,
    )


def process_multi_client(args: argparse.Namespace) -> int:
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        accounts = load_accounts_from_xlsx(
            args.accounts_xlsx,
            args.client_id_col,
            args.name_col,
            args.checked_col,
            args.max_accounts,
        )
    except Exception as exc:  # noqa: BLE001 - command line entrypoint should report cleanly.
        print(f"Failed to read accounts workbook: {exc}", file=sys.stderr)
        return 1

    if args.resume_from_latest_manifest:
        accounts = skip_accounts_before_latest_manifest(accounts, output_root, args)

    print(f"Loaded {len(accounts)} checked accounts from {windows_to_wsl_path(args.accounts_xlsx)}")
    for index, account in enumerate(accounts, start=1):
        row = process_account(account, index, len(accounts), args, output_root)
        append_account_manifest_row(output_root, row)
    print(f"Saved accounts manifest to {output_root / 'accounts_manifest.tsv'}")
    return 0


def main() -> int:
    global COMMAND_TIMEOUT_SECONDS, DOWNLOAD_TIMEOUT_SECONDS, DOWNLOAD_START_TIMEOUT_SECONDS, DOWNLOAD_STALL_TIMEOUT_SECONDS

    args = parse_args()
    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    if args.max_accounts is not None and args.max_accounts < 1:
        print("--max-accounts must be at least 1", file=sys.stderr)
        return 2
    if args.resume_verify_last_runs < 0:
        print("--resume-verify-last-runs must be 0 or greater", file=sys.stderr)
        return 2
    if args.command_timeout < 0:
        print("--command-timeout must be 0 or greater", file=sys.stderr)
        return 2
    if args.download_timeout < 0:
        print("--download-timeout must be 0 or greater", file=sys.stderr)
        return 2
    if args.download_start_timeout < 0:
        print("--download-start-timeout must be 0 or greater", file=sys.stderr)
        return 2
    if args.download_stall_timeout < 0:
        print("--download-stall-timeout must be 0 or greater", file=sys.stderr)
        return 2

    COMMAND_TIMEOUT_SECONDS = args.command_timeout or None
    DOWNLOAD_TIMEOUT_SECONDS = args.download_timeout or None
    DOWNLOAD_START_TIMEOUT_SECONDS = args.download_start_timeout or None
    DOWNLOAD_STALL_TIMEOUT_SECONDS = args.download_stall_timeout or None

    wrapper_path = Path(args.wrapper)
    if not wrapper_path.exists():
        print(f"Wrapper not found: {wrapper_path}", file=sys.stderr)
        return 2

    if args.accounts_xlsx:
        return process_multi_client(args)
    return process_single_client(args)


if __name__ == "__main__":
    raise SystemExit(main())
