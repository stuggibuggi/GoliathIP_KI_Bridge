"""Account model discovery and explicit compatibility checks; no secrets cached."""
import asyncio
import base64
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import websockets
from PIL import Image

# Built-in voices are documented, not returned by GET /models.
VOICES = ('alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse', 'marin', 'cedar')


class OptionsError(RuntimeError):
    pass


def validate(key, model=None):
    if not isinstance(key, str) or not key.strip():
        raise OptionsError('Kein API-Key eingetragen')
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', model)):
        raise OptionsError('Ungueltige Modell-ID')


def api_json(key, path, payload=None):
    validate(key)
    req = urllib.request.Request('https://api.openai.com/v1/' + path,
        data=None if payload is None else json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key.strip(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Do not return raw provider bodies, URLs or credentials to logs/browser.
        raise OptionsError(f'OpenAI HTTP {error.code}: Zugriff, Modell und API-Berechtigung pruefen') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise OptionsError('OpenAI nicht erreichbar oder ungueltige Antwort') from None


def fetch_catalog(key):
    result = api_json(key, 'models')
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        raise OptionsError('Ungueltige Modellliste')
    ids = sorted({item['id'] for item in result['data'] if isinstance(item, dict)
                  and isinstance(item.get('id'), str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', item['id'])})
    realtime = [model for model in ids if 'realtime' in model.lower()
                and not any(word in model.lower() for word in ('transcribe', 'whisper', 'translate'))]
    vision = [model for model in ids if model.startswith(('gpt-4o', 'gpt-4.1', 'gpt-5', 'gpt-6', 'o1', 'o3', 'o4'))
              and not any(word in model.lower() for word in
                          ('realtime', 'audio', 'transcribe', 'tts', 'search', 'codex', 'deep-research'))]
    return {'all_models': ids, 'realtime_models': realtime, 'vision_models': vision,
            'voices': list(VOICES), 'note': 'API-Liste; Kandidaten nach Modellnamen gefiltert, Kompatibilitaet separat testen.'}


async def _test_realtime(key, model, voice):
    url = 'wss://api.openai.com/v1/realtime?model=' + urllib.parse.quote(model, safe='')
    async with websockets.connect(url, additional_headers={'Authorization': 'Bearer ' + key.strip()},
                                  open_timeout=10, close_timeout=2, max_size=2**20) as ws:
        await ws.send(json.dumps({'type': 'session.update', 'session': {
            'type': 'realtime', 'model': model, 'output_modalities': ['audio'],
            'audio': {'input': {'format': {'type': 'audio/pcmu'}, 'turn_detection': None},
                      'output': {'format': {'type': 'audio/pcmu'}, 'voice': voice}}}}))
        for _ in range(30):
            event = json.loads(await ws.recv())
            if event.get('type') == 'error':
                raise OptionsError('Realtime lehnt Modell/Stimme/G.711-Konfiguration ab')
            if event.get('type') == 'session.updated':
                session = event.get('session', {})
                audio = session.get('audio', {})
                if audio.get('output', {}).get('voice') != voice:
                    raise OptionsError('Realtime hat die ausgewaehlte Stimme nicht bestaetigt')
                if any(audio.get(direction, {}).get('format', {}).get('type') != 'audio/pcmu'
                       for direction in ('input', 'output')):
                    raise OptionsError('Realtime hat G.711 PCMU nicht bestaetigt')
                return 'Modell/Stimme und G.711 bestaetigt; keine Sprachausgabe erzeugt'
        raise OptionsError('Keine Realtime-Sitzungsbestaetigung erhalten')


def test_realtime(key, model, voice):
    if model is None:
        raise OptionsError('Keine Modell-ID ausgewaehlt')
    validate(key, model)
    if voice not in VOICES:
        raise OptionsError('Unbekannte eingebaute Realtime-Stimme')
    try:
        return asyncio.run(asyncio.wait_for(_test_realtime(key, model, voice), timeout=20))
    except OptionsError:
        raise
    except Exception:
        raise OptionsError('Realtime-Test fehlgeschlagen: Verbindung, Modellzugriff und Stimme pruefen') from None


def test_vision(key, model):
    if model is None:
        raise OptionsError('Keine Modell-ID ausgewaehlt')
    validate(key, model)
    # Only an artificial image: never the actual camera or private images.
    image = io.BytesIO()
    Image.new('RGB', (64, 64), 'blue').save(image, format='PNG')
    result = api_json(key, 'responses', {'model': model, 'store': False,
        'max_output_tokens': 128, 'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Name the main color in one word.'},
            {'type': 'input_image', 'detail': 'low', 'image_url':
             'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode('ascii')}]}]})
    text = ' '.join(part.get('text', '') for item in result.get('output', [])
                    for part in item.get('content', []) if part.get('type') == 'output_text').strip()
    if not text:
        raise OptionsError('Bildmodell lieferte keinen Text; Modell/Tokenlimit pruefen')
    return 'Bildmodell-Test erfolgreich (synthetisches Testbild, keine Kameraaufnahme)'
