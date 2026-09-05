"""Workday ATS scraper.

Uses Workday's public career-site CXS endpoints.

A configured company slug should be the public Workday career-site URL,
for example:

    https://acme.wd5.myworkdayjobs.com/AcmeCareers

The public jobs endpoint is:

    POST https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs

Individual postings can be fetched from the externalPath returned by the
jobs endpoint.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from src.intake.base import BaseScraper, ScraperError
from src.intake.html_utils import strip_html
from src.intake.schema import RawJob, classify_employment_type, classify_seniority

logger = logging.getLogger("autoapply.intake.workday")

DEFAULT_PAGE_SIZE = 20
MAX_PAGES = 500


class WorkdayScraper(BaseScraper):
    """Scraper for public Workday career sites."""

    source_name = "workday"

    def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        """Fetch all currently published jobs from a Workday career site.

        Args:
            company_slug: Public Workday career-site URL, e.g.
                "https://acme.wd5.myworkdayjobs.com/AcmeCareers"

        Returns:
            List of normalized RawJob objects.
        """
        tenant, base_url, site = _parse_workday_url(company_slug)

        logger.info(
            "Fetching Workday jobs for '%s' (%s/%s)",
            company_slug,
            tenant,
            site,
        )

        jobs: list[RawJob] = []
        offset = 0

        for _ in range(MAX_PAGES):
            data = self._fetch_page(
                base_url=base_url,
                tenant=tenant,
                site=site,
                offset=offset,
            )

            raw_jobs = data.get("jobPostings", [])
            if not isinstance(raw_jobs, list):
                raise ScraperError(
                    f"Unexpected Workday jobs response for {company_slug}: "
                    "'jobPostings' is not a list"
                )

            if not raw_jobs:
                break

            for item in raw_jobs:
                if not isinstance(item, dict):
                    continue

                try:
                    jobs.append(
                        self._parse_job(
                            company_slug=company_slug,
                            base_url=base_url,
                            tenant=tenant,
                            site=site,
                            item=item,
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "Skipping malformed Workday job %s: %s",
                        item.get("jobReqId") or item.get("externalPath"),
                        e,
                    )

            if len(raw_jobs) < DEFAULT_PAGE_SIZE:
                break

            offset += DEFAULT_PAGE_SIZE

        else:
            logger.warning(
                "Reached Workday pagination limit for %s",
                company_slug,
            )

        logger.info(
            "Fetched %d jobs from Workday/%s",
            len(jobs),
            company_slug,
        )
        return jobs

    def fetch_job(self, company_slug: str, job_id: str) -> RawJob:
        """Fetch one Workday job by its Workday requisition ID.

        Workday's public detail endpoint is keyed by externalPath, so we
        first fetch the board and find the matching posting.
        """
        logger.info("Fetching Workday job %s/%s", company_slug, job_id)

        for job in self.fetch_jobs(company_slug):
            if job.source_id == str(job_id):
                return job

        raise ScraperError(
            f"Workday job {company_slug}/{job_id} was not found "
            "on the public board"
        )

    def _fetch_page(
        self,
        *,
        base_url: str,
        tenant: str,
        site: str,
        offset: int,
    ) -> dict:
        """Fetch one page of Workday job postings."""
        url = f"{base_url}/wday/cxs/{tenant}/{site}/jobs"

        try:
            response = self._client.post(
                url,
                json={
                    "appliedFacets": {},
                    "limit": DEFAULT_PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            if isinstance(e, ScraperError):
                raise
            raise ScraperError(
                f"Failed to fetch Workday jobs from {url}: {e}"
            ) from e

        if not isinstance(data, dict):
            raise ScraperError(
                f"Unexpected Workday response shape from {url}: "
                f"{type(data)}"
            )

        return data

    def _parse_job(
        self,
        *,
        company_slug: str,
        base_url: str,
        tenant: str,
        site: str,
        item: dict,
    ) -> RawJob:
        """Convert a Workday posting into RawJob."""
        source_id = _extract_source_id(item)
        if not source_id:
            raise ValueError("missing Workday job ID")

        title = str(item.get("title", "")).strip()
        if not title:
            raise ValueError("missing Workday job title")

        external_path = str(item.get("externalPath", "")).strip()

        job_url = _build_job_url(
            base_url=base_url,
            tenant=tenant,
            site=site,
            external_path=external_path,
        )

        location = _extract_location(item)
        employment_type = _extract_employment_type(item)

        description = _extract_description(item)

        # Workday listing responses sometimes contain enough information
        # for the posting itself, but the detail endpoint is where the full
        # description normally lives.
        detail_data = None
        if external_path:
            try:
                detail_data = self._fetch_detail(
                    base_url=base_url,
                    tenant=tenant,
                    site=site,
                    external_path=external_path,
                )
            except ScraperError as e:
                logger.debug(
                    "Could not fetch Workday details for %s: %s",
                    source_id,
                    e,
                )

        if detail_data:
            job_info = detail_data.get("jobPostingInfo")
            if isinstance(job_info, dict):
                title = (
                    str(job_info.get("title") or title).strip()
                    or title
                )

                detail_location = _extract_detail_location(job_info)
                if detail_location:
                    location = detail_location

                detail_employment = _extract_detail_employment_type(job_info)
                if detail_employment:
                    employment_type = detail_employment

                detail_description = _extract_detail_description(job_info)
                if detail_description:
                    description = detail_description

                detail_external_path = str(
                    job_info.get("externalPath") or external_path
                ).strip()

                if detail_external_path:
                    job_url = _build_job_url(
                        base_url=base_url,
                        tenant=tenant,
                        site=site,
                        external_path=detail_external_path,
                    )

        return RawJob(
            source="workday",
            source_id=source_id,
            company=_infer_company_name(company_slug, item, detail_data),
            title=title,
            location=location or None,
            employment_type=classify_employment_type(employment_type),
            seniority=classify_seniority(title),
            description=description,
            application_url=job_url or None,
            ats_type="workday",
            raw_data={
                "listing": item,
                "detail": detail_data,
            },
        )

    def _fetch_detail(
        self,
        *,
        base_url: str,
        tenant: str,
        site: str,
        external_path: str,
    ) -> dict:
        """Fetch a single public Workday job detail response."""
        external_path = external_path.lstrip("/")
        url = f"{base_url}/wday/cxs/{tenant}/{site}/{external_path.lstrip('/')}"

        try:
            response = self._client.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise ScraperError(
                f"Failed to fetch Workday job details from {url}: {e}"
            ) from e

        if not isinstance(data, dict):
            raise ScraperError(
                f"Unexpected Workday detail response from {url}: "
                f"{type(data)}"
            )

        return data


def _parse_workday_url(company_slug: str) -> tuple[str, str, str]:
    """Return tenant, base URL, and career-site name from a Workday URL."""
    value = company_slug.strip()

    if not value:
        raise ScraperError("Workday company slug is empty")

    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"

    parsed = urlparse(value)

    if not parsed.hostname:
        raise ScraperError(f"Invalid Workday URL: {company_slug}")

    host_parts = parsed.hostname.split(".")

    # Expected:
    #   tenant.wd5.myworkdayjobs.com
    #
    # Some sites use additional host components, so only require the
    # myworkdayjobs.com suffix and take the first host component as tenant.
    if len(host_parts) < 3 or not parsed.hostname.endswith("myworkdayjobs.com"):
        raise ScraperError(
            f"Not a recognized Workday career URL: {company_slug}"
        )

    tenant = host_parts[0]

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        raise ScraperError(
            f"Workday career URL is missing its site name: {company_slug}"
        )

    site = path_parts[0]

    base_url = f"{parsed.scheme}://{parsed.netloc}"

    return tenant, base_url, site


def _extract_source_id(item: dict) -> str:
    """Extract the most stable Workday-native job identifier."""
    for key in ("jobReqId", "jobRequisitionId", "id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    # externalPath is still stable enough to identify the posting if
    # Workday omitted the requisition ID.
    value = item.get("externalPath")
    if value is not None and str(value).strip():
        return str(value).strip().rstrip("/").split("/")[-1]

    return ""


def _extract_location(item: dict) -> str:
    """Extract location text from a Workday listing."""
    for key in ("locationsText", "location"):
        value = item.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, list):
            locations = [
                str(entry).strip()
                for entry in value
                if str(entry).strip()
            ]
            if locations:
                return " | ".join(dict.fromkeys(locations))

    return ""


def _extract_employment_type(item: dict) -> str:
    """Extract employment type from Workday listing fields."""
    for key in (
        "timeType",
        "workerSubType",
        "employmentType",
        "employment_type",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _extract_description(item: dict) -> str | None:
    """Extract a description from a Workday listing if present."""
    for key in (
        "jobDescription",
        "jobDescriptionHtml",
        "description",
    ):
        value = item.get(key)

        if isinstance(value, str) and value.strip():
            if "<" in value and ">" in value:
                return strip_html(value)

            return value.strip()

    return None


def _extract_detail_location(job_info: dict) -> str:
    """Extract location from Workday jobPostingInfo."""
    for key in ("location", "locationsText"):
        value = job_info.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, list):
            locations = [
                str(entry).strip()
                for entry in value
                if str(entry).strip()
            ]
            if locations:
                return " | ".join(dict.fromkeys(locations))

    return ""


def _extract_detail_employment_type(job_info: dict) -> str:
    """Extract employment type from Workday jobPostingInfo."""
    for key in (
        "timeType",
        "workerSubType",
        "employmentType",
    ):
        value = job_info.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _extract_detail_description(job_info: dict) -> str | None:
    """Extract the full Workday job description."""
    for key in (
        "jobDescription",
        "jobDescriptionHtml",
        "description",
    ):
        value = job_info.get(key)

        if isinstance(value, str) and value.strip():
            if "<" in value and ">" in value:
                return strip_html(value)

            return value.strip()

    return None


def _build_job_url(
    *,
    base_url: str,
    tenant: str,
    site: str,
    external_path: str,
) -> str:
    """Build the public Workday posting URL."""
    if not external_path:
        return ""

    path = external_path.lstrip("/")

    # externalPath normally looks like:
    #   /job/foo/bar
    #
    # The public posting is hosted directly at:
    #   https://tenant.wd5.myworkdayjobs.com/site/job/foo/bar
    if path.startswith("job/"):
        return f"{base_url}/{site}/{path}"

    return f"{base_url}/{site}/{path}"


def _infer_company_name(
    company_slug: str,
    listing: dict,
    detail_data: dict | None,
) -> str:
    """Use a company name exposed by Workday when available."""
    candidates: list[object] = [
        listing.get("companyName"),
        listing.get("company"),
        listing.get("organizationName"),
        listing.get("organization"),
    ]

    if isinstance(detail_data, dict):
        job_info = detail_data.get("jobPostingInfo")
        if isinstance(job_info, dict):
            candidates.extend(
                [
                    job_info.get("companyName"),
                    job_info.get("company"),
                    job_info.get("organizationName"),
                    job_info.get("organization"),
                ]
            )

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    parsed = urlparse(
        company_slug
        if company_slug.startswith(("http://", "https://"))
        else f"https://{company_slug}"
    )

    hostname = parsed.hostname or company_slug

    tenant = hostname.split(".")[0]
    return tenant.replace("-", " ").replace("_", " ").title()
