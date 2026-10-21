"""Bounded field conversion and atomic, recoverable JSON configuration files."""
import json
import os
import shutil
import tempfile


def safe_int(value, default, minimum, maximum):
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 12 or not text.lstrip('-').isdigit():
            return default
        try:
            value = int(text)
        except ValueError:
            return default
    if not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def safe_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        if value in ('false', '0', 'no', 'off'):
            return False
    if type(value) is int and value in (0, 1):
        return bool(value)
    return default


def safe_text(value, default='', limit=4096):
    return value[:limit].replace('\x00', '') if isinstance(value, str) else default


def _read_object(path):
    with open(path, 'r', encoding='utf-8-sig') as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError('配置根节点必须是对象')
    return data


def read_config(path):
    """Preserve an unreadable primary before any later autosave repairs it."""
    try:
        return _read_object(path), ''
    except (OSError, ValueError, UnicodeError) as error:
        if not os.path.exists(path) and not os.path.exists(path + '.bak'):
            return None, ''
        preserved = ''
        if os.path.isfile(path):
            try:
                fd, preserved = tempfile.mkstemp(prefix='config-corrupt-', suffix='.json',
                                                dir=os.path.dirname(os.path.abspath(path)))
                os.close(fd)
                shutil.copyfile(path, preserved)
            except OSError:
                preserved = ''
        try:
            data = _read_object(path + '.bak')
            notice = '配置文件无法读取，已从上一次备份恢复。'
        except (OSError, ValueError, UnicodeError):
            data = None
            notice = '配置文件无法读取，已使用默认设置。'
        if preserved:
            notice += '\n原文件已保留：' + preserved
        else:
            notice += '\n' + str(error)
        return data, notice


def _atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.qtimer-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_config(path, data):
    try:
        previous = _read_object(path)
    except FileNotFoundError:
        previous = None
    except OSError as error:
        # Being able to replace an unreadable file is not permission to lose it.
        return False, str(error)
    except (ValueError, UnicodeError):
        previous = None
    # Keep the preceding valid JSON. A corrupt primary must never overwrite it.
    if previous is not None:
        try:
            _atomic_write(path + '.bak', previous)
        except (OSError, ValueError, TypeError):
            pass  # Backup failure must not prevent saving the primary.
    try:
        _atomic_write(path, data)
    except (OSError, ValueError, TypeError) as error:
        return False, str(error)
    if not os.path.exists(path + '.bak'):
        try:
            _atomic_write(path + '.bak', data)
        except (OSError, ValueError, TypeError):
            pass
    return True, ''
