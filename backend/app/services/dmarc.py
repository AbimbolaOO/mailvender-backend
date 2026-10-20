"""DMARC aggregate reports (RFC 7489 appendix C): unpack and parse them safely.

Receivers (Google, Microsoft, Yahoo, …) send one report a day to the `rua=` address,
usually as a zipped or gzipped XML attachment. Each <record> says how many messages a
source IP sent as our domain and whether they passed DKIM/SPF (DMARC-aligned).

Attachments come from the internet, so:
* archives are size- and file-count-limited while unpacking (zip bombs);
* XML with a DOCTYPE or ENTITY declaration is refused, so entity expansion and
  external entities (XXE) are impossible.
"""

import io
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree import ElementTree

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 5
MAX_RECORDS = 20_000


class DmarcError(Exception):
    """The attachment isn't a usable DMARC report."""


@dataclass
class DmarcRecord:
    source_ip: str
    count: int
    disposition: str
    dkim: str
    spf: str


@dataclass
class DmarcReport:
    reporter: str
    report_id: str
    domain: str
    begin_at: datetime
    end_at: datetime
    records: list[DmarcRecord] = field(default_factory=list)

    @property
    def messages(self) -> int:
        return sum(r.count for r in self.records)

    @property
    def passed(self) -> int:
        return sum(r.count for r in self.records if "pass" in (r.dkim, r.spf))


def looks_like_report(filename: str, content_type: str) -> bool:
    name = filename.lower()
    return name.endswith((".xml", ".zip", ".gz", ".xml.gz")) or content_type in (
        "application/zip", "application/x-zip-compressed", "application/gzip", "application/x-gzip",
        "text/xml", "application/xml",
    )


def unpack(filename: str, content_type: str, data: bytes) -> list[bytes]:
    """The XML document(s) inside an attachment, within the size limits."""
    name = filename.lower()
    if name.endswith(".zip") or content_type in ("application/zip", "application/x-zip-compressed"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            raise DmarcError("Not a valid zip file.") from error
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ARCHIVE_FILES:
            raise DmarcError("The zip file holds too many files.")
        documents = []
        for info in members:
            if info.file_size > MAX_XML_BYTES:
                raise DmarcError("The report is too large.")
            with archive.open(info) as handle:
                content = handle.read(MAX_XML_BYTES + 1)  # the header's size may lie
            if len(content) > MAX_XML_BYTES:
                raise DmarcError("The report is too large.")
            documents.append(content)
        return documents
    if name.endswith(".gz") or content_type in ("application/gzip", "application/x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            content = inflater.decompress(data, MAX_XML_BYTES + 1)
        except zlib.error as error:
            raise DmarcError("Not a valid gzip file.") from error
        if len(content) > MAX_XML_BYTES or inflater.unconsumed_tail:
            raise DmarcError("The report is too large.")
        return [content]
    if len(data) > MAX_XML_BYTES:
        raise DmarcError("The report is too large.")
    return [data]


def _text(node: ElementTree.Element, path: str, default: str = "") -> str:
    value = node.findtext(path)
    return value.strip() if value else default


def _epoch(value: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, OverflowError, OSError) as error:
        raise DmarcError("The report's date range is invalid.") from error


def parse(xml: bytes) -> DmarcReport:
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        raise DmarcError("Reports with DOCTYPE or ENTITY declarations are refused.")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise DmarcError("The report isn't valid XML.") from error
    if root.tag != "feedback":
        raise DmarcError("Not a DMARC aggregate report.")
    report = DmarcReport(
        reporter=_text(root, "report_metadata/org_name", "unknown")[:200],
        report_id=_text(root, "report_metadata/report_id", "")[:200],
        domain=_text(root, "policy_published/domain", "")[:253].lower(),
        begin_at=_epoch(_text(root, "report_metadata/date_range/begin", "0")),
        end_at=_epoch(_text(root, "report_metadata/date_range/end", "0")),
    )
    for record in root.findall("record")[:MAX_RECORDS]:
        try:
            count = int(_text(record, "row/count", "0"))
        except ValueError:
            count = 0
        report.records.append(DmarcRecord(
            source_ip=_text(record, "row/source_ip", "unknown")[:64],
            count=max(count, 0),
            disposition=_text(record, "row/policy_evaluated/disposition", "none")[:16].lower(),
            dkim=_text(record, "row/policy_evaluated/dkim", "fail")[:16].lower(),
            spf=_text(record, "row/policy_evaluated/spf", "fail")[:16].lower(),
        ))
    return report
