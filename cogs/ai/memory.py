from cogs.ai.storage import load_json, update_json

FILE = "data/memory.json"
MAX_MESSAGES = 4


def get_history(user_id):
    data = load_json(FILE, dict)
    return data.get(str(user_id), [])


def add_to_history(user_id, role, content, name=None):
    if role not in ("user", "assistant"):
        return

    uid = str(user_id)

    def mutate(data):
        entry = {"role": role, "content": content}
        if role == "user" and name:
            entry["name"] = name
        data.setdefault(uid, []).append(entry)
        data[uid] = data[uid][-MAX_MESSAGES:]

    update_json(FILE, mutate, dict)


def clear_history(user_id):
    uid = str(user_id)
    update_json(FILE, lambda data: data.update({uid: []}), dict)


def clear_all_history():
    update_json(FILE, lambda data: data.clear(), dict)