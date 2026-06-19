# Unified with bancho.py's canonical BanchoBot (id 1). bancho.py owns this row in
# the shared `users`/`lazer_users` tables (see app/state/services.py sync triggers);
# g0v0 points at the same id so both servers share ONE bot account.
BANCHOBOT_ID = 1

BACKUP_CODE_LENGTH = 10

NEW_SCORE_FORMAT_VER = 20220705
SUPPORT_TOTP_VERIFICATION_VER = 20250913

# Maximum score in standardised scoring mode
# https://github.com/ppy/osu/blob/master/osu.Game/Rulesets/Scoring/ScoreProcessor.cs
MAX_SCORE = 1000000
