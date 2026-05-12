"""Биба — модуль-разведчик функционала VK Ads.

Биба не лезет в реальные кампании Vizit'а — он опрашивает справочные
endpoints VK API и собирает inventory возможностей платформы.
Результат складывается в docs/biba_findings/ и используется Claude'ом
для понимания «что вообще можно делать в кабинете».
"""

from src.biba.persona import BIBA_BANNER, BIBA_SIGNOFF, REPORT_STYLE_RULES

__all__ = ["BIBA_BANNER", "BIBA_SIGNOFF", "REPORT_STYLE_RULES"]
