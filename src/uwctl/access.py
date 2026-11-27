"""Let whoever is sitting at the laptop use uwctld without needing the
``uwctl`` group (and therefore without logging out and back in).

Like systemd's ``uaccess`` for sound and video devices: the daemon reads
logind's session list and adds a POSIX ACL entry on its socket for every user
with an *active, local* session (not SSH, not the login screen).  Members of
the ``uwctl`` group keep access as before.
"""

from __future__ import annotations

import errno
import logging
import os
import struct

log = logging.getLogger("uwctl")

SESSIONS_DIR = os.environ.get("UWCTL_LOGIND_DIR", "/run/systemd/sessions")
ACL_XATTR = "system.posix_acl_access"
_VERSION = 2
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
UNDEFINED_ID = 0xFFFFFFFF
RW = 0o6


def local_session_uids(sessions_dir: str = SESSIONS_DIR) -> set[int]:
    uids: set[int] = set()
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return uids
    for name in names:
        if name.endswith(".ref"):
            continue
        kv = {}
        try:
            with open(os.path.join(sessions_dir, name)) as fh:
                for line in fh:
                    if "=" in line:
                        k, v = line.rstrip("\n").split("=", 1)
                        kv[k] = v
        except OSError:
            continue
        if (kv.get("ACTIVE") == "1" and kv.get("REMOTE", "0") == "0" and kv.get("SEAT")
                and kv.get("CLASS", "user") == "user" and kv.get("UID", "").isdigit()):
            uids.add(int(kv["UID"]))
    return uids


def acl_bytes(uids, group_rw: bool = True) -> bytes:
    """Binary ``system.posix_acl_access`` value: owner rw, group rw (or none),
    rw for each uid, mask rw, other none."""
    entries = [(USER_OBJ, RW, UNDEFINED_ID)]
    entries += [(USER, RW, uid) for uid in sorted(uids)]
    entries += [(GROUP_OBJ, RW if group_rw else 0, UNDEFINED_ID),
                (MASK, RW, UNDEFINED_ID),
                (OTHER, 0, UNDEFINED_ID)]
    data = struct.pack("<I", _VERSION)
    for tag, perm, ident in entries:
        data += struct.pack("<HHI", tag, perm, ident)
    return data


class SocketAccess:
    def __init__(self, path: str, group_rw: bool, sessions_dir: str = SESSIONS_DIR) -> None:
        self.path = path
        self.group_rw = group_rw
        self.sessions_dir = sessions_dir
        self.current: set[int] | None = None
        self.enabled = True

    def refresh(self) -> None:
        if not self.enabled:
            return
        uids = local_session_uids(self.sessions_dir) - {0}
        if uids == self.current:
            return
        try:
            if uids:
                os.setxattr(self.path, ACL_XATTR, acl_bytes(uids, self.group_rw))
            else:
                try:
                    os.removexattr(self.path, ACL_XATTR)
                except OSError as exc:
                    if exc.errno != errno.ENODATA:
                        raise
                os.chmod(self.path, 0o660 if self.group_rw else 0o600)
            self.current = uids
            log.info("socket access for local users: %s", sorted(uids) or "none")
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOTSUP):
                self.enabled = False
                log.info("no ACL support on %s - only root and the 'uwctl' group can connect",
                         os.path.dirname(self.path))
            else:
                log.warning("could not update socket access: %s", exc)
