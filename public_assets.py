"""Narrow orbit/DEM asset URLs; CopDEM and geoid transfers are anonymous."""
import re
from urllib.parse import urlsplit

GEOID_URL = 'https://cdn.proj.org/us_nga_egm08_25.tif'


def public_asset_kind(url):
    try:parsed = urlsplit(url)
    except (ValueError, TypeError):return None
    if parsed.scheme != 'https' or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    try:
        if parsed.port not in (None, 443):return None
    except ValueError:return None
    if parsed.hostname == 's1qc.asf.alaska.edu' and re.fullmatch(
        r'/aux_(?:poeorb|resorb)/S1[A-D]_OPER_AUX_(?:POEORB|RESORB)_OPOD_\d{8}T\d{6}_V\d{8}T\d{6}_\d{8}T\d{6}\.EOF', parsed.path):
        return 'orbit'
    if parsed.hostname == 'copernicus-dem-30m.s3.amazonaws.com' and re.fullmatch(
        r'/(Copernicus_DSM_COG_10_[NS]\d{2}_00_[EW]\d{3}_00_DEM)/\1\.tif', parsed.path):
        return 'dem'
    if url in (GEOID_URL, 'https://cdn.proj.org/us_nga_egm96_15.tif'):return 'geoid'
    return None
