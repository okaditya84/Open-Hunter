"""Applicant Tracking System (ATS) clients.

Most companies host their jobs on a small number of ATS platforms that expose
clean, public JSON APIs. Reading those APIs is dramatically more accurate and
reliable than scraping rendered HTML, and it sidesteps bot-detection entirely
because these endpoints are designed to be consumed. This package is the
backbone of the pipeline's accuracy.
"""

from .base import ATSClient, ALL_CLIENTS, get_client

__all__ = ["ATSClient", "ALL_CLIENTS", "get_client"]
