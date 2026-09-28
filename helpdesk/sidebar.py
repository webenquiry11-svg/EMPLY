"""
helpdesk/sidebar.py
"""

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as trans


def safe_reverse(name, *args, **kwargs):
    try:
        return reverse(name, *args, **kwargs)
    except NoReverseMatch:
        return "#"


MENU = trans("Help Desk")
IMG_SRC = "images/ui/headset-solid.svg"

SUBMENUS = [
    {
        "menu": trans("FAQs"),
        "redirect": safe_reverse("faq-category-view"),
    },
    {
        "menu": trans("Support Tickets"),
        "redirect": safe_reverse("support-ticket-list"),
    },
    {
        "menu": trans("Tickets"),
        "redirect": safe_reverse("ticket-view"),
        "accessibility": "helpdesk.sidebar.tickets_accessibility",
    },
]


def tickets_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.is_superuser
