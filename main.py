"""Main script to execute directly."""

import os
import logging
import asyncio
from datetime import datetime
from email.mime.text import MIMEText

from aiosmtplib import send as send_email
from jinja2 import Template
from slack_sdk.webhook import WebhookClient

from envidat.utils import get_logger, load_dotenv_if_in_debug_mode

load_dotenv_if_in_debug_mode(env_file=".env.secret")
get_logger()
from envidat.s3.bucket import Bucket  # noqa: E402


log = logging.getLogger(__name__)


def render_jinja_template(template_path, **context):
    """Render a jinja2 html file."""
    with open(template_path) as file:
        template = Template(file.read())
    return template.render(context)


async def send_s3_status_email(smtp_server, smtp_email, bucket_size_dict):
    """Send S3 status email to EnviDat admin."""
    message = MIMEText(
        render_jinja_template(
            "./email_templates/s3_status_email.html",
            bucket_size_dict=bucket_size_dict,
        ),
        "html",
    )
    message["From"] = smtp_email
    message["To"] = smtp_email
    message["Subject"] = "S3 Status Update"
    log.debug("Sending S3 status update to EnviDat admin")
    await send_email(message, hostname=smtp_server, port=25)


async def send_s3_warning_email(smtp_server, smtp_email, bucket_name, size_gb):
    """Send S3 warning email to EnviDat admin."""
    log.debug("Creating email message with MIMEText Jinja template.")
    message = MIMEText(
        render_jinja_template(
            "./email_templates/s3_warning_email.html",
            bucket_name=bucket_name,
            size_gb=size_gb,
        ),
        "html",
    )
    message["From"] = smtp_email
    message["To"] = smtp_email
    message["Subject"] = f"WARNING: Bucket {bucket_name} > 20TB"
    log.debug(f"Sending S3 warning to EnviDat admin for bucket {bucket_name}")
    await send_email(message, hostname=smtp_server, port=25)


def trigger_slack_webhook(message: str):
    """Notify slack channel bucket size exceeds threshhold."""
    if "SLACK_WEBHOOK" in os.environ:
        log.debug("Getting slack webhook url from environment variable.")
        webhook_url = os.getenv("SLACK_WEBHOOK")

        webhook = WebhookClient(webhook_url)

        log.debug("Sending slack notification.")
        webhook.send(text=message)
    else:
        log.warning("SLACK_WEBHOOK variable not in environment. Skipping...")


async def main():
    """For direct execution of file."""
    log.info("Starting main s3-monitoring script.")

    # Get smtp env vars
    if "SMTP_EMAIL" not in os.environ:
        log.error("Environment variable SMTP_EMAIL not set.")
        raise OSError("Environment variable SMTP_EMAIL not set.")
    if "SMTP_SERVER" not in os.environ:
        log.error("Environment variable SMTP_SERVER not set.")
        raise OSError("Environment variable SMTP_SERVER not set.")
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_server = os.getenv("SMTP_SERVER")

    bucket_size_dict = {}
    all_buckets = Bucket.list_buckets()
    # Don't check envicloud
    if "envicloud" in all_buckets:
        all_buckets.remove("envicloud")

    for bucket in all_buckets:
        s3_bucket = Bucket(bucket_name=bucket)
        log.debug(f"Getting size of bucket {bucket}.")
        bucket_size_gb = s3_bucket.size(items_per_page=100000) / 1024 / 1024 / 1024
        bucket_size_dict[bucket] = round(bucket_size_gb, 2)
    log.info(f"Bucket sizes: {bucket_size_dict}")

    # Only send status updates on Thursday
    force_status = os.getenv("FORCE_STATUS_UPDATE", default=None)
    if force_status or datetime.today().weekday() == 3:
        log.info("Sending bucket status email.")
        await send_s3_status_email(smtp_server, smtp_email, bucket_size_dict)
    else:
        log.info("Not sending bucket status email. Only on Thursday.")

    # Only run multipart cleanup on Sunday
    force_cleanup = os.getenv("FORCE_MULTIPART_CLEANUP", default=None)
    if force_cleanup or datetime.today().weekday() == 6:
        log.info("Cleaning up bucket multiparts.")
        for bucket in all_buckets:
            s3_bucket = Bucket(bucket_name=bucket)
            s3_bucket.clean_multiparts()
    else:
        log.info("Not cleaning up bucket multiparts. Only on Sunday.")

    # Trigger warning if bucket size exceeds 20TB
    size_warning_triggered = False
    for bucket_name, size_gb in bucket_size_dict.items():
        if size_gb > 20000:
            log.warning(f"Bucket {bucket_name} is currently {size_gb}GB. Quota=20TB.")
            await send_s3_warning_email(smtp_server, smtp_email, bucket_name, size_gb)
            await trigger_slack_webhook(
                f"WARNING: Bucket {bucket_name} is currently {size_gb}GB, "
                "exceeding 20TB quota."
            )
            size_warning_triggered = True
    if not size_warning_triggered:
        log.info("All buckets are below 20TB, no issues.")

    log.info("Finished main s3-monitoring script.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
