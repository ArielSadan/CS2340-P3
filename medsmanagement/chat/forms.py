from pathlib import Path

from django import forms
from pypdf import PdfReader

from .models import Document
from .services.preprocessing import decode_text

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_CHARS = 100000


class DocumentForm(forms.ModelForm):
    content = forms.CharField(
        required=False,
        label="Document text",
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="Paste the article or dataset text, or upload a file below.",
    )
    upload = forms.FileField(
        required=False,
        label="Upload a document",
        widget=forms.ClearableFileInput(attrs={"accept": ".txt,.csv,.json,.pdf,.html,.htm"}),
        help_text="TXT, CSV, JSON, HTML, or a PDF with selectable text. Maximum 5 MB. A file replaces the text above.",
    )

    class Meta:
        model = Document
        fields = ["title", "source_type", "source_url", "content", "file_name"]
        widgets = {"file_name": forms.HiddenInput()}
        labels = {"source_url": "Source URL (optional)"}
        help_texts = {
            "title": "Use a readable article or dataset title. This appears in chatbot citations.",
            "source_url": "Saved as a citation link. Paste or upload the actual content; this page does not download URLs.",
        }

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("content", "")
        upload = cleaned.get("upload")
        if upload:
            if upload.size > MAX_UPLOAD_BYTES:
                self.add_error("upload", "Choose a file smaller than 5 MB.")
                return cleaned
            extension = Path(upload.name).suffix.lower()
            try:
                if extension == ".pdf":
                    reader = PdfReader(upload)
                    if reader.is_encrypted:
                        self.add_error("upload", "Upload a PDF without password protection.")
                        return cleaned
                    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
                elif extension in [".txt", ".csv", ".json", ".html", ".htm"]:
                    text = decode_text(upload.read())
                else:
                    self.add_error("upload", "Use a TXT, CSV, JSON, HTML, or PDF file.")
                    return cleaned
            except Exception:
                self.add_error("upload", "Could not read this file. Try a supported text file or a PDF with selectable text.")
                return cleaned

        text = text.strip()
        if not text:
            self.add_error("content", "Paste document text or upload a file with readable text.")
        elif len(text) > MAX_DOCUMENT_CHARS:
            self.add_error("content", "Limit each document to 100,000 characters. Split larger datasets into smaller files.")
        cleaned["content"] = text
        return cleaned

    def save(self, commit=True):
        document = super().save(commit=False)
        upload = self.cleaned_data.get("upload")
        if upload:
            # Store extracted text and a display name, not an executable upload.
            document.file_name = upload.name
        if commit:
            document.save()
        return document
