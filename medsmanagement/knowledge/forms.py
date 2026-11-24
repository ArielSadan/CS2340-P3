import re

from django import forms

from chat.models import PromptStyle


class PromptStyleForm(forms.ModelForm):
    class Meta:
        model = PromptStyle
        fields = ["phrases", "instructions"]
        labels = {"phrases": "Matching phrases", "instructions": "Response instructions"}
        widgets = {
            "phrases": forms.Textarea(attrs={"rows": 2, "class": "dev__urls dev__prompt-field"}),
            "instructions": forms.Textarea(attrs={"rows": 2, "class": "dev__urls dev__prompt-field"}),
        }

    def clean_phrases(self):
        phrases = []
        for line in self.cleaned_data["phrases"].splitlines():
            if not line.strip():
                continue
            phrase = " ".join(re.findall(r"\w+", line.lower()))
            if not phrase:
                raise forms.ValidationError("Use words for each matching phrase.")
            if phrase not in phrases:
                phrases.append(phrase)
        return "\n".join(phrases)
