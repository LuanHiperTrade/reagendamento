# rules_engine/templatetags/custom_filters.py

from django import template

register = template.Library()

@register.filter(name='split')
def split(value, arg):
    """Permite dividir strings no template HTML (Ex: "Segunda,Terça"|split:",")"""
    if value:
        return value.split(arg)
    return []