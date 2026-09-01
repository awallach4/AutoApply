"""Ashby ATS scraper.

Uses Ashby's public Job Posting API (no auth required):
  GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}

Public job boards are hosted at:
  https://jobs.ashbyhq.com/{job_board_name}

Ashby's public endpoint returns the current job board in one response,
including hosted job URLs, apply URLs, and published descriptions.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from src.intake.base import BaseScraper, ScraperError
from src.intake.html_utils import strip_html
from src.intake.schema import RawJob, classify_employment_type, classify_seniority

logger = logging.getLogger("autoapply.intake.ashby")

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyScraper(BaseScraper):
    """Scraper for public Ashby job boards."""

    source_name = "ashby"

    def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        """Fetch all currently published jobs for an Ashby job board."""
        url = f"{BASE_URL}/{quote(company_slug, safe='')}"
        logger.info("Fetching Ashby jobs for '%s'", company_slug)

        try:
            data = self._get(url).json()
        except ScraperError:
            raise
        except Exception as e:
            raise ScraperError(
                f"Failed to parse Ashby response for {company_slug}: {e}"
            ) from e

        if not isinstance(data, dict):
            raise ScraperError(
                f"Unexpected Ashby response shape for {company_slug}: {type(data)}"
            )

        raw_jobs_list = data.get("jobs", [])
        if not isinstance(raw_jobs_list, list):
            raise ScraperError(
                f"Unexpected Ashby jobs response for {company_slug}"
            )

        jobs: list[RawJob] = []
        for item in raw_jobs_list:
            if not isinstance(item, dict):
                continue

            # Public consumers should only expose listed postings.
            if item.get("isListed") is False:
                continue

            try:
                jobs.append(self._parse_job(company_slug, item))
            except Exception as e:
                logger.warning(
                    "Skipping malformed Ashby job %s: %s",
                    item.get("id"),
                    e,
                )

        logger.info("Fetched %d jobs from Ashby/%s", len(jobs), company_slug)
        return jobs

    def fetch_job(self, company_slug: str, job_id: str) -> RawJob:
        """Fetch one Ashby job by fetching its board and matching its ID.

        Ashby's public posting API does not expose a public single-job GET
        endpoint, so this intentionally fetches the board once.
        """
        logger.info("Fetching Ashby job %s/%s", company_slug, job_id)

        for job in self.fetch_jobs(company_slug):
            if job.source_id == str(job_id):
                return job

        raise ScraperError(
            f"Ashby job {company_slug}/{job_id} was not found on the public board"
        )

    def _parse_job(self, company_slug: str, item: dict) -> RawJob:
        """Convert an Ashby public API job dict to RawJob."""
        source_id = str(item.get("id", "")).strip()
        if not source_id:
            raise ValueError("missing job id")

        title = str(item.get("title", "")).strip()
        if not title:
            raise ValueError("missing job title")

        location = _extract_location(item)
        employment_raw = item.get("employmentType") or item.get("employment_type") or title
        description = _extract_description(item)

        job_url = (
            item.get("jobUrl")
            or f"https://jobs.ashbyhq.com/{quote(company_slug, safe='')}/{source_id}"
        )
        apply_url = item.get("applyUrl") or job_url

        return RawJob(
            source="ashby",
            source_id=source_id,
            company=_infer_company_name(company_slug, item),
            title=title,
            location=location or None,
            employment_type=classify_employment_type(str(employment_raw)),
            seniority=classify_seniority(title),
            description=description,
            application_url=apply_url,
            ats_type="ashby",
            raw_data=item,
        )


def _extract_description(item: dict) -> str | None:
    """Prefer Ashby's plain-text description, falling back to HTML."""
    plain = item.get("descriptionPlain")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()

    for key in ("descriptionHtml", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return strip_html(value)

    return None


def _extract_location(item: dict) -> str:
    """Extract the primary location plus useful secondary locations."""
    locations: list[str] = []

    primary = item.get("location")
    if isinstance(primary, str) and primary.strip():
        locations.append(primary.strip())

    secondary = item.get("secondaryLocations", [])
    if isinstance(secondary, list):
        for entry in secondary:
            if isinstance(entry, str):
                value = entry.strip()
            elif isinstance(entry, dict):
                value = str(entry.get("location", "")).strip()
            else:
                value = ""

            if value and value not in locations:
                locations.append(value)

    if locations:
        return " | ".join(locations)

    workplace_type = item.get("workplaceType")
    if isinstance(workplace_type, str) and workplace_type.strip():
        return workplace_type.strip()

    if item.get("isRemote") is True:
        return "Remote"

    return ""


def _infer_company_name(slug: str, item: dict) -> str:
    """Use a company name when exposed; otherwise format the slug."""
    for key in ("companyName", "company", "organizationName", "organization"):
        value = item.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    return slug.replace("-", " ").replace("_", " ").title()
