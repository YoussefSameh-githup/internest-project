from django import forms
from django.utils.translation import gettext_lazy as _

from .models import LoungeComment, LoungePost


class PostForm(forms.ModelForm):
    class Meta:
        model = LoungePost
        fields = ["title", "body", "category"]
        labels = {"title": _("Title"), "body": _("What would you like to discuss?"), "category": _("Tag")}
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "maxlength": 160}),
            "body": forms.Textarea(attrs={"class": "form-control", "rows": 4, "maxlength": 5000}),
            "category": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = [("", _("No tag"))] + list(LoungePost.CATEGORY_CHOICES)
        self.fields["title"].required = False


class CommentForm(forms.ModelForm):
    parent_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = LoungeComment
        fields = ["body"]
        labels = {"body": _("Comment")}
        widgets = {"body": forms.Textarea(attrs={"class": "form-control", "rows": 2, "maxlength": 2000})}
