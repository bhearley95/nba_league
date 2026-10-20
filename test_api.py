from datetime import datetime

from nba_api.stats.endpoints import playergamelog


PLAYER_ID = "1628369"  # Jayson Tatum
PLAYER_NAME = "Jayson Tatum"
SEASON = "2024-25"
GAME_DATE = datetime.strptime("2024-10-22", "%Y-%m-%d").date()

game_log = playergamelog.PlayerGameLog(
    player_id=PLAYER_ID,
    season=SEASON,
    season_type_all_star="Regular Season",
    timeout=20,
).get_data_frames()[0]

game_log["GAME_DATE"] = game_log["GAME_DATE"].apply(
    lambda value: datetime.strptime(value, "%b %d, %Y").date()
)
game = game_log.loc[game_log["GAME_DATE"] == GAME_DATE]

if game.empty:
    print(f"No {SEASON} regular-season game found for {PLAYER_NAME} on {GAME_DATE}.")
else:
    row = game.iloc[0]
    print(f"{PLAYER_NAME} scored {int(row['PTS'])} points on {GAME_DATE} ({row['MATCHUP']}).")
