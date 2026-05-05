import os
import json

# Aliases file lives in project/.tmp alongside other cached state
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT_ROOT, '.tmp')
ALIASES_FILE = os.path.join(CACHE_DIR, 'ticker_aliases.json')

DEFAULT_ALIASES = {
    "GOOGL": "GOOG",
    "BRK.B": "BRK.A",
    "BRK-B": "BRK.A",
    "BF.B": "BF.A",
    "DISCK": "DISCA",
    "UAA": "UA",
    "FOXA": "FOX",
    "NWSA": "NWS",
    "ZILL": "Z"
}

_aliases_cache = None

def _load_aliases():
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache

    if not os.path.exists(CACHE_DIR):
        try:
            os.makedirs(CACHE_DIR)
        except Exception:
            pass

    if not os.path.exists(ALIASES_FILE):
        try:
            with open(ALIASES_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_ALIASES, f, indent=4)
            _aliases_cache = DEFAULT_ALIASES
            return _aliases_cache
        except Exception:
            return DEFAULT_ALIASES

    try:
        with open(ALIASES_FILE, 'r', encoding='utf-8') as f:
            _aliases_cache = json.load(f)
    except Exception:
        _aliases_cache = DEFAULT_ALIASES

    return _aliases_cache

def update_aliases(new_dict):
    """
    Merges new aliases into the existing ticker_aliases.json file.
    Does nothing if all new aliases are already present.
    """
    aliases = _load_aliases()
    changed = False
    
    for k, v in new_dict.items():
        if k not in aliases or aliases[k] != v:
            aliases[k] = v
            changed = True
            
    if changed:
        global _aliases_cache
        _aliases_cache = aliases
        if not os.path.exists(CACHE_DIR):
            try:
                os.makedirs(CACHE_DIR)
            except Exception:
                pass
        try:
            with open(ALIASES_FILE, 'w', encoding='utf-8') as f:
                json.dump(aliases, f, indent=4)
        except Exception as e:
            print(f"Error saving updated aliases: {e}")

def resolve_ticker(ticker):
    """
    Returns the canonical ticker for a given ticker.
    (e.g., GOOGL -> GOOG).
    Also sanitizes the input to uppercase and standardizes delimiters.
    """
    if not ticker:
        return ticker
        
    aliases = _load_aliases()
    
    # Clean it up first
    cleaned = str(ticker).strip().upper()
    
    # Many systems use periods or dashes interchangeably, try both
    # If the exact cleaned version is in aliases, use it
    if cleaned in aliases:
        return aliases[cleaned]
        
    # Check if a dash version is aliased
    dashed = cleaned.replace('.', '-')
    if dashed in aliases:
        return aliases[dashed]
        
    # Check if a dot version is aliased
    dotted = cleaned.replace('-', '.')
    if dotted in aliases:
        return aliases[dotted]
        
    return cleaned
