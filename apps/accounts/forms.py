from io import BytesIO

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, ImageOps

from .models import User

AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_MAX_PIXELS = 25_000_000
AVATAR_SIZE = 256


class RegisterForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "email")


def process_avatar(upload):
    """Square-crop to 256x256 and re-encode as JPEG.

    Re-encoding drops EXIF/GPS metadata and anything odd hidden in the original file,
    and keeps every stored avatar small no matter what was uploaded.
    """
    try:
        img = Image.open(upload)
        if img.width * img.height > AVATAR_MAX_PIXELS:
            raise ValidationError("That image's dimensions are too large.")
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            flat = Image.new("RGB", img.size, "white")
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
        img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("We couldn't read that image. Try a JPG or PNG.")
    buf = BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return ContentFile(buf.getvalue(), name="avatar.jpg")


class ProfileForm(forms.ModelForm):
    remove_avatar = forms.BooleanField(required=False, label="Remove my photo")

    class Meta:
        model = User
        fields = ("display_name", "avatar")
        labels = {"display_name": "Display name", "avatar": "Profile photo"}
        help_texts = {"display_name": "Shown to your friends instead of your username. Optional."}
        widgets = {"avatar": forms.FileInput(attrs={"accept": "image/*"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._old_avatar = self.instance.avatar.name

    def clean_avatar(self):
        f = self.cleaned_data.get("avatar")
        if isinstance(f, UploadedFile):
            if f.size > AVATAR_MAX_BYTES:
                raise ValidationError("Please choose an image under 5 MB.")
            return process_avatar(f)
        return f

    def save(self, commit=True):
        user = super().save(commit=False)
        uploaded = bool(self.files.get("avatar"))
        if self.cleaned_data.get("remove_avatar") and not uploaded:
            user.avatar = ""
        if commit:
            user.save()
            if self._old_avatar and self._old_avatar != user.avatar.name:
                default_storage.delete(self._old_avatar)
        return user
