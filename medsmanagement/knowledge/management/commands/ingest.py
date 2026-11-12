import os
 
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
 
from knowledge.ingestion.sources import ingest_uploaded_file, ingest_url
 
 
class Command(BaseCommand):
    help = "Ingest files and/or URLs into the knowledge base."
 
    def add_arguments(self, parser):
        parser.add_argument("items", nargs="+", help="File paths or http(s) URLs")
 
    def handle(self, *args, **options):
        failures = 0
        for item in options["items"]:
            if item.startswith(("http://", "https://")):
                result = ingest_url(item)
            elif os.path.isfile(item):
                with open(item, "rb") as fh:
                    upload = SimpleUploadedFile(os.path.basename(item), fh.read())
                result = ingest_uploaded_file(upload)
            else:
                self.stderr.write(self.style.ERROR(f"✗ {item}: not a URL or an existing file"))
                failures += 1
                continue
            line = f"{result.status.upper():8} {item}: {result.message}"
            if result.ok:
                self.stdout.write(self.style.SUCCESS(line))
            else:
                failures += 1
                self.stderr.write(self.style.ERROR(line))
        if failures:
            self.stderr.write(f"{failures} item(s) failed.")