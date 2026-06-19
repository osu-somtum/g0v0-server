#!/usr/bin/env python3
"""Binary-patch osu.Server.Spectator.dll for the Somtum dual-bancho deploy.

The upstream googuteam/osu-server-spectator image (`:v2`) gates *friend online
presence* on this query (DatabaseAccess.GetUserFriendsAsync):

    SELECT r.target_id FROM relationship r
    JOIN lazer_users u ON r.target_id = u.id
    WHERE r.user_id = @UserId AND r.type = 'Friend' AND u.priv = 1

Two things are wrong for our shared stable+lazer DB:

  1. `r.type = 'Friend'` — g0v0's RelationshipType is FOLLOW="friend"/BLOCK="block",
     and the live `relationship.type` column is enum('FOLLOW','BLOCK') storing
     'FOLLOW'. 'Friend' matches nothing, so GetUserFriendsAsync returns empty for
     everyone and the metadata hub never emits FriendPresenceUpdated.
  2. `u.priv = 1` — native g0v0 users have priv=1, but bancho-bridged users carry
     bancho's privilege *bitmask* (e.g. 64695, 2103, 51), never literally 1, so
     this also excludes every bridged account. The unrestricted check should be a
     bit test: `(u.priv & 1) = 1` (bancho UNRESTRICTED = 1<<0). 1&1=1 keeps native
     users passing too.

Both edits are case/character swaps that preserve the exact UTF-16 length, so we
can patch the compiled #US-heap string in place without rebuilding the .NET
project (which would otherwise need the private `g0v0.osu.Game` GitHub-Packages
feed + a PAT):

    'Friend' AND u.priv = 1   ->   'FOLLOW' AND u.priv&1=1   (23 chars each)

`&` binds tighter than `=` in MySQL, so `u.priv&1=1` parses as `(u.priv & 1) = 1`.

Idempotent: re-running on an already-patched DLL is a no-op.
"""

import sys

OLD = "'Friend' AND u.priv = 1".encode("utf-16-le")
NEW = "'FOLLOW' AND u.priv&1=1".encode("utf-16-le")
assert len(OLD) == len(NEW)


def main(path: str) -> int:
    data = bytearray(open(path, "rb").read())
    if data.count(NEW) >= 1 and data.count(OLD) == 0:
        print("patch_dll: already patched, nothing to do")
        return 0
    n = data.count(OLD)
    if n != 1:
        print(f"patch_dll: ERROR expected exactly 1 occurrence of target, found {n}", file=sys.stderr)
        return 1
    data = data.replace(OLD, NEW)
    open(path, "wb").write(data)
    print("patch_dll: patched friend-presence query (Friend->FOLLOW, priv=1 -> priv&1=1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
