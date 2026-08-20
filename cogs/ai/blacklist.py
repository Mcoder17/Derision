from cogs.ai.storage import load_json, save_json

FILE = "data/blacklist.json"

_blacklist = load_json(FILE, list)


def is_blacklisted(user_id):
    return user_id in _blacklist


def add_to_blacklist(user_id):
    if user_id not in _blacklist:
        _blacklist.append(user_id)
        save_json(FILE, _blacklist)


def remove_from_blacklist(user_id):
    if user_id in _blacklist:
        _blacklist.remove(user_id)
        save_json(FILE, _blacklist)


def get_blacklist():
    return list(_blacklist)