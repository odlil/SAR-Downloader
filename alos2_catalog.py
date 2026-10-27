"""ASF's public ALOS-2 ScanSAR L1.1 collection, queried explicitly through CMR.

Collection/attribute definitions: C3315903479-ASF (ALOS2_L1_PSR2).
Do not send ALOS-2 to the legacy SearchAPI: it can silently ignore that dataset.
"""
from urllib.parse import urlsplit, unquote

COLLECTION = 'ALOS2_L1_PSR2'


def _ranges(value):
    return [tuple(map(int, token.split('-'))) for token in str(value).split(',') if token]


def cmr_query(params, area):
    # A single covering bbox is conservative; the common adapter performs exact
    # polygon/hole intersection, then enforces the requested number lists below.
    query = [('provider', 'ASF'), ('short_name', COLLECTION),
             ('page_size', params['maxResults']), ('sort_key[]', '-start_date'),
             ('bounding_box', ','.join(f'{value:.10f}' for value in area.bounds))]
    if params.get('start') or params.get('end'):
        query.append(('temporal', f"{params.get('start', '')},{params.get('end', '')}"))
    for key, attribute in (('relativeOrbit', 'PATH_NUMBER'), ('asfframe', 'FRAME_NUMBER')):
        if params.get(key):
            values = [number for interval in _ranges(params[key]) for number in interval]
            interval = str(min(values)) if min(values) == max(values) else f'{min(values)},{max(values)}'
            query.append(('attribute[]', f'int,{attribute},{interval}'))
    if params.get('flightDirection'):
        query.append(('attribute[]', 'string,ASCENDING_DESCENDING,' + params['flightDirection']))
    if params.get('polarization'):
        # Dual-pol values are separate UMM attribute values. Narrow using one
        # component and verify the exact polarization combination locally.
        query.append(('attribute[]', 'string,POLARIZATION,' + params['polarization'].split('+')[0]))
    return query


def normalize_granule(entry):
    meta, umm = entry['meta'], entry['umm']
    if meta.get('provider-id') != 'ASF' or umm['CollectionReference']['ShortName'] != COLLECTION:
        raise ValueError('unexpected ALOS-2 collection')
    if not any(platform.get('ShortName') == 'ALOS-2' for platform in umm.get('Platforms', [])):
        raise ValueError('unexpected platform')
    attributes = {item['Name']: item.get('Values', []) for item in umm.get('AdditionalAttributes', [])}
    def first(name):
        return next(iter(attributes.get(name, [])), None)
    if first('PROCESSING_LEVEL') not in ('1.1', 'L1.1'):
        raise ValueError('unexpected processing level')
    polygons = []
    geometry = umm['SpatialExtent']['HorizontalSpatialDomain']['Geometry']
    def ring(boundary):
        points = [[point['Longitude'], point['Latitude']] for point in boundary['Points']]
        if points and points[0] != points[-1]:
            points.append(points[0])
        return points
    for polygon in geometry.get('GPolygons', []):
        rings = [ring(polygon['Boundary'])]
        rings.extend(ring(item.get('Boundary', item)) for item in polygon.get('ExclusiveZone', {}).get('Boundaries', []))
        polygons.append(rings)
    if not polygons:
        raise ValueError('missing polygon')
    footprint = {'type': 'MultiPolygon', 'coordinates': polygons}
    url = next((item['URL'] for item in umm.get('RelatedUrls', [])
                if item.get('Type') == 'GET DATA' and item.get('URL', '').startswith('https://')
                and urlsplit(item['URL']).path.lower().endswith('.zip')), None)
    if not url:
        raise ValueError('missing downloadable ZIP')
    from earthdata_auth import validate_url, TransferError
    try:
        validate_url(url, asset=True)
    except TransferError as error:
        raise ValueError('unexpected distribution host') from error
    filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    size = next((item.get('SizeInBytes') for item in umm.get('DataGranule', {}).get('ArchiveAndDistributionInformation', [])
                 if item.get('Name') == filename), None)
    dates = umm['TemporalExtent']['RangeDateTime']
    return {'type': 'Feature', 'geometry': footprint, 'properties': {
        'fileID': umm['GranuleUR'], 'sceneName': umm['GranuleUR'], 'fileName': filename,
        'platform': 'ALOS-2', 'processingLevel': 'L1.1', 'url': url, 'bytes': size,
        'startTime': dates['BeginningDateTime'], 'stopTime': dates.get('EndingDateTime'),
        'pathNumber': first('PATH_NUMBER'), 'frameNumber': first('FRAME_NUMBER'),
        'flightDirection': first('ASCENDING_DESCENDING'), 'beamModeType': first('BEAM_MODE'),
        'polarization': '+'.join(attributes.get('POLARIZATION', [])),
        'orbit': next((item.get('OrbitNumber') for item in umm.get('OrbitCalculatedSpatialDomains', [])), None),
    }}


def matches_filters(scene, payload):
    for key, field in (('path', 'path'), ('frame', 'frame')):
        if payload.get(key):
            from asf_api import number_filter
            ranges = _ranges(number_filter(payload[key]))
            value = scene[field]
            if value is None or not any(interval[0] <= value <= interval[-1] for interval in ranges):
                return False
    if payload.get('direction') and scene['direction'] != payload['direction']:
        return False
    polarization = payload.get('polarization')
    return not polarization or set(scene['polarization'].upper().split('+')) == set(polarization.upper().split('+'))
