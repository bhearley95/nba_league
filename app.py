from datetime import date, timedelta
import hmac
import hashlib
import os
from pathlib import Path

import pandas as pd
import streamlit as st

import database
from overview_service import OverviewRefresh
from weekly_score_service import WeeklyScoreRefresh
from score_service import (
    HISTORY_WARNING,
    get_week_schedule,
    normalize_nba_team_name,
    refresh_player_logs_for_week,
    season_for_week,
    update_scores,
)


FANTASY_SEASON_START = date(2026, 10, 19)
FANTASY_SEASON_WEEKS = 25
ADMIN_PASSWORD = "nbaleague"


def validate_admin_password() -> None:
    provided = st.session_state.get("overview_admin_password", "")
    st.session_state["overview_admin_unlocked"] = bool(
        hmac.compare_digest(provided, ADMIN_PASSWORD)
    )
    st.session_state["overview_admin_auth_result"] = (
        "unlocked" if st.session_state["overview_admin_unlocked"] else "invalid"
    )
    # Do not retain the submitted password in Streamlit session state.
    st.session_state["overview_admin_password"] = ""


st.set_page_config(
    page_title="NBA Fantasy League",
    page_icon="🏀",
    layout="wide",
)

database.CLOUD_URL = os.environ.get("SQLITECLOUD_URL", "")
if not database.CLOUD_URL:
    try:
        database.CLOUD_URL = st.secrets.get("SQLITECLOUD_URL", "")
    except st.errors.StreamlitSecretNotFoundError:
        pass


@st.cache_resource
def initialize_storage(storage_key: str) -> None:
    database.initialize_database()


storage_key = hashlib.sha256((database.CLOUD_URL or str(database.DATABASE_PATH)).encode()).hexdigest()
try:
    initialize_storage(storage_key)
except Exception:
    st.error("Could not open the league database. Check the configured database connection and permissions.")
    st.stop()


@st.cache_resource
def sync_catalog(source_path: str, modified_at: float, storage_key: str) -> int:
    # The modification time is part of the cache key, so edits to the source file
    # are imported the next time Streamlit reruns. Matching version 2 also
    # invalidates older in-process cache entries after the jersey collision fix.
    del modified_at
    return database.sync_player_catalog(database.read_player_positions_file(source_path))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def numeric_column_config(frame, formats: dict[str, str] | None = None) -> dict:
    """Left-align numeric columns in Streamlit tables, preserving their types."""
    formats = formats or {}
    return {
        column: st.column_config.NumberColumn(
            alignment="left", format=formats.get(column)
        )
        for column in frame.select_dtypes(include="number").columns
    }


@st.cache_resource
def weekly_score_refresh(storage_key: str) -> WeeklyScoreRefresh:
    return WeeklyScoreRefresh()


def render_score_warnings(warnings) -> None:
    for warning in dict.fromkeys(warnings):
        if warning == HISTORY_WARNING:
            st.caption("Historical game logs are delayed. Totals use saved game logs and the latest available live scores.")
        else:
            st.warning(warning)


@st.fragment(run_every="30s")
def render_weekly_scores(team_id: int, team_name: str, week_start: date) -> None:
    """Render one team's grid and refresh live boxscores while it is visible."""
    roster = [dict(row) for row in database.get_week_active_roster(team_id, week_start)]
    if not roster:
        st.info("No active roster has been submitted for this team and week.")
        return

    _, scheduled_games, warnings, refreshing = weekly_score_refresh(storage_key).poll(team_id, week_start)
    if refreshing:
        st.caption("Updating scores and schedule in the background. Showing saved game logs until the next refresh.")
    render_score_warnings(warnings)

    scores, game_logs = database.get_week_score_snapshot(team_id, week_start)
    player_names = [player["full_name"] for player in roster]
    player_name_by_id = {player["player_id"]: player["full_name"] for player in roster}
    grid = pd.DataFrame("—", index=range(7), columns=player_names)
    played_cells = set()
    live_cells = {}
    player_totals = {player_name: 0 for player_name in player_names}
    for score in scores:
        player_name = player_name_by_id.get(score["player_id"])
        if player_name is not None:
            player_totals[player_name] = score["points"]
    for game in game_logs:
        game_day = date.fromisoformat(game["game_date"])
        day_index = (game_day - week_start).days
        if not 0 <= day_index < 7:
            continue
        player_name = game["full_name"]
        grid.loc[day_index, player_name] = str(game["points"])
        played_cells.add((day_index, player_name))
        if game["status"] == "Live":
            live_cells[(day_index, player_name)] = "Live"

    days = [week_start + timedelta(days=offset) for offset in range(7)]
    scheduled_cells = set()
    for player in roster:
        player_name = player["full_name"]
        nba_team = normalize_nba_team_name(player["nba_team_name"])
        for game_day, team_name_for_game in scheduled_games:
            day_index = (game_day - week_start).days
            if (
                normalize_nba_team_name(team_name_for_game) == nba_team
                and 0 <= day_index < 7
                and (day_index, player_name) not in played_cells
                and (day_index, player_name) not in live_cells
            ):
                scheduled_cells.add((day_index, player_name))

    grid.loc[7] = [str(player_totals[name]) for name in player_names]
    display_days = [day.strftime("%a %m/%d") for day in days] + ["Weekly total"]
    grid.insert(0, "Day", display_days)
    total_row = 7

    def mark_game_cells(data):
        # Pastel backgrounds need an explicit dark foreground in either theme.
        styles = pd.DataFrame("", index=data.index, columns=data.columns)
        for row, player_name in played_cells:
            styles.loc[row, player_name] = "background-color: #d9f2df; color: #172033; font-weight: bold"
        for row, player_name in scheduled_cells:
            styles.loc[row, player_name] = "background-color: #dbeafe; color: #172033"
        for (row, player_name), status in live_cells.items():
            color = "#ffd699" if status == "Live" else "#d9f2df"
            styles.loc[row, player_name] = f"background-color: {color}; color: #172033; font-weight: bold"
        styles.loc[total_row, :] = "font-weight: bold; border-top: 2px solid #888"
        return styles

    weekly_total = sum(player_totals.values())
    st.metric(f"{team_name} weekly total", weekly_total)
    st.caption(
        "Live games refresh every 30 seconds while this page is open. "
        "Orange cells are in progress, green cells are finished, and blue cells are scheduled."
    )
    st.dataframe(
        grid.style.apply(mark_game_cells, axis=None),
        hide_index=True,
        width="stretch",
        height=370,
    )


@st.cache_resource
def overview_refresh(storage_key: str) -> OverviewRefresh:
    return OverviewRefresh(weekly_score_refresh(storage_key))


@st.fragment(run_every="30s")
def render_overview(week_start: date, as_of: date) -> None:
    """Show saved scores immediately while refreshing NBA data in the background."""
    st.title("League Overview")
    week_end = week_start + timedelta(days=6)
    st.caption(
        f"Current scoring week: {week_start:%b} {week_start.day} – "
        f"{week_end:%b} {week_end.day}, {week_end.year}"
    )
    st.caption("Scores refresh every 30 seconds while this page is open.")

    warnings, refreshing = overview_refresh(storage_key).poll(week_start)
    if refreshing:
        st.caption("Updating NBA scores in the background. Showing saved scores until the next refresh.")

    leaderboard = database.get_leaderboard(week_start, as_of)
    if leaderboard:
        st.subheader("League leaderboard")
        leaderboard_frame = pd.DataFrame([dict(team) for team in leaderboard])
        leaderboard_frame = leaderboard_frame.rename(
            columns={
                "name": "Team",
                "total_points": "Total Points",
                "current_week_points": "This Week",
                "weekly_wins": "Weekly Wins",
            }
        )[["Team", "Total Points", "This Week", "Weekly Wins"]]
        st.dataframe(
            leaderboard_frame,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Total Points": st.column_config.NumberColumn(format="%d", alignment="left"),
                "This Week": st.column_config.NumberColumn(format="%d", alignment="left"),
                "Weekly Wins": st.column_config.NumberColumn(format="%d", alignment="left"),
            },
        )
        st.caption(
            "Weekly wins are counted after the week ends and once every team has a complete score; tied teams each receive a win."
        )
    else:
        st.info("No league teams have been added yet. Add teams in League Setup to get started.")

    render_score_warnings(warnings)

    st.divider()
    st.subheader("Admin access")
    with st.form("overview_admin_login", clear_on_submit=True):
        st.text_input(
            "Password",
            type="password",
            key="overview_admin_password",
        )
        login_submitted = st.form_submit_button(
            "Unlock admin controls",
            on_click=validate_admin_password,
        )
    if login_submitted:
        # Overview is a timed fragment. Re-run the full app so the sidebar
        # reflects the newly updated authorization state immediately.
        st.rerun(scope="app")

    if st.session_state.get("overview_admin_unlocked", False):
        st.success("Admin controls are unlocked for this session.")
        st.caption("League storage: SQLite Cloud" if database.CLOUD_URL else "League storage: local SQLite file")
        with st.expander("Download league backup"):
            st.caption("Save a copy of teams, rosters, lineups, and scores before migrating storage.")
            if st.button("Prepare database backup", key="prepare_league_backup"):
                st.session_state["league_backup"] = database.export_database_backup()
            if "league_backup" in st.session_state:
                st.download_button(
                    "Download database backup",
                    data=st.session_state["league_backup"],
                    file_name="nba_league_backup.db",
                    mime="application/vnd.sqlite3",
                    on_click="ignore",
                )
        if st.button("Lock admin controls", key="lock_overview_admin"):
            st.session_state["overview_admin_unlocked"] = False
            st.session_state["overview_admin_auth_result"] = None
            st.session_state.pop("league_backup", None)
            st.rerun()
    elif st.session_state.get("overview_admin_auth_result") == "invalid":
        st.error("Incorrect password.")


player_file = Path(__file__).with_name("player_positions.txt")
catalog_error = None
catalog_count = 0
if player_file.exists():
    try:
        catalog_count = sync_catalog(str(player_file), player_file.stat().st_mtime, storage_key)
    except Exception as exc:
        catalog_error = str(exc)
else:
    catalog_error = "player_positions.txt was not found beside app.py."


with st.sidebar:
    st.title("🏀 NBA Fantasy")
    menu_pages = ["Overview", "Rosters", "Weekly Scores", "Schedule Grid", "Free Agents"]
    selected_page = st.session_state.get("selected_page", "Overview")
    for menu_page in menu_pages:
        if st.button(
            menu_page,
            key=f"nav_{menu_page.lower().replace(' ', '_')}",
            type="primary" if selected_page == menu_page else "secondary",
            width="stretch",
        ):
            st.session_state["selected_page"] = menu_page
            st.rerun()
    st.divider()
    admin_unlocked = st.session_state.get("overview_admin_unlocked", False)
    if st.button(
        "League Setup",
        key="nav_league_setup",
        type="primary" if selected_page == "League Setup" else "secondary",
        width="stretch",
        disabled=not admin_unlocked,
    ):
        st.session_state["selected_page"] = "League Setup"
        st.rerun()
    if not admin_unlocked:
        st.caption("Unlock admin access on Overview to open League Setup.")
    page = st.session_state.get("selected_page", "Overview")


if page == "League Setup":
    st.title("League Setup")
    st.write(
        "Add league members and manage each team's ten-player roster. Set an effective "
        "week for every change. Earlier weeks keep their saved roster, lineup, and scores; "
        "the selected roster carries forward until another effective-week snapshot is saved."
    )

    if catalog_error:
        st.error(f"Could not load the NBA player list: {catalog_error}")
    else:
        st.caption(f"{catalog_count} players loaded from player_positions.txt")

    st.subheader("Add a team")
    with st.form("add_league_team", clear_on_submit=True):
        member_name = st.text_input("League member's name")
        add_team = st.form_submit_button("Add team")
    if add_team:
        try:
            database.create_team(member_name)
            st.success(f"Added {member_name.strip()} to the league.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    teams = [dict(team) for team in database.get_teams()]
    if not teams:
        st.info("Add at least one league member to start entering rosters.")
    elif not catalog_error:
        st.divider()
        st.subheader("Manage a team's roster")
        scoring_weeks = [
            FANTASY_SEASON_START + timedelta(weeks=index)
            for index in range(FANTASY_SEASON_WEEKS)
        ]
        week_labels = [
            f"Week #{index + 1} ({week:%m/%d/%y}-{(week + timedelta(days=6)):%m/%d/%y})"
            for index, week in enumerate(scoring_weeks)
        ]
        current_week_index = min(
            max((date.today() - FANTASY_SEASON_START).days // 7, 0),
            FANTASY_SEASON_WEEKS - 1,
        )
        team_column, week_column = st.columns(2)
        with team_column:
            selected_team = st.selectbox(
                "Choose a league team",
                teams,
                format_func=lambda team: team["name"],
                key="setup_team",
            )
        with week_column:
            effective_week_label = st.selectbox(
                "Effective week",
                week_labels,
                index=current_week_index,
                key="setup_effective_week",
            )
        effective_week = scoring_weeks[week_labels.index(effective_week_label)]
        team_id = selected_team["id"]
        saved_roster = database.get_team_roster(team_id, effective_week)
        saved_ids = [row["player_id"] for row in saved_roster]
        players = database.get_active_players()
        player_by_id = {player["player_id"]: player for player in players}
        unavailable_ids = database.get_players_rostered_by_other_teams(
            effective_week, team_id
        )

        if not players:
            st.warning("No players were imported from player_positions.txt.")
        else:
            st.write(
                "Each NBA player can be rostered by one league team during the selected week."
            )
            left_column, right_column = st.columns(2)
            selected_ids = []
            used_ids = set()

            def player_label(player_id):
                if player_id is None:
                    return "Select a player"
                player = player_by_id[player_id]
                return (
                    f"{player['full_name']} | {player['nba_team_name']} "
                    f"| {player['position']}"
                )

            for slot in range(10):
                key = f"team_roster_{team_id}_{effective_week.isoformat()}_{slot}"
                initial_value = saved_ids[slot] if slot < len(saved_ids) else None
                if key not in st.session_state:
                    st.session_state[key] = initial_value

                options = [None] + [
                    player_id
                    for player_id in player_by_id
                    if player_id not in unavailable_ids and player_id not in used_ids
                ]
                if st.session_state[key] not in options:
                    st.session_state[key] = None

                column = left_column if slot < 5 else right_column
                with column:
                    chosen_id = st.selectbox(
                        f"Player {slot + 1}",
                        options,
                        format_func=player_label,
                        key=key,
                    )
                selected_ids.append(chosen_id)
                if chosen_id is not None:
                    used_ids.add(chosen_id)

            complete = all(player_id is not None for player_id in selected_ids)
            if st.button("Save roster from this week", type="primary", disabled=not complete):
                try:
                    database.save_team_roster(team_id, effective_week, selected_ids)
                    st.success(
                        f"Saved {selected_team['name']}'s roster effective {effective_week:%b %d, %Y}."
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

            if not complete:
                remaining = 10 - sum(player_id is not None for player_id in selected_ids)
                st.caption(f"Select {remaining} more players to save.")

        st.subheader(f"{selected_team['name']}'s roster for {effective_week_label}")
        current_roster = database.get_team_roster(team_id, effective_week)
        if current_roster:
            roster_frame = pd.DataFrame([dict(row) for row in current_roster]).rename(
                columns={
                    "full_name": "Player",
                    "nba_team_name": "NBA Team",
                    "position": "Position",
                    "buybacks": "Buybacks",
                }
            )[["Player", "NBA Team", "Position", "Buybacks"]]
            st.dataframe(
                roster_frame,
                hide_index=True,
                use_container_width=True,
                column_config=numeric_column_config(roster_frame),
            )
        else:
            st.info("No roster has been saved for this team yet.")

        st.subheader("League rosters for the selected week")
        for team in teams:
            roster = database.get_team_roster(team["id"], effective_week)
            with st.expander(f"{team['name']} | {len(roster)}/10 players"):
                if roster:
                    team_frame = pd.DataFrame([dict(row) for row in roster]).rename(
                        columns={
                            "full_name": "Player",
                            "nba_team_name": "NBA Team",
                            "position": "Position",
                            "buybacks": "Buybacks",
                        }
                    )[["Player", "NBA Team", "Position", "Buybacks"]]
                    st.dataframe(
                        team_frame,
                        hide_index=True,
                        use_container_width=True,
                        column_config=numeric_column_config(team_frame),
                    )
                else:
                    st.caption("No roster entered for this week yet.")

elif page == "Overview":
    week_start = monday_of(date.today())
    render_overview(week_start, date.today())

elif page == "Rosters":
    st.title("Weekly Rosters")
    st.write(
        "Set each team's active lineup for a Monday-through-Sunday week. "
        "This five-player lineup is selected from that week's ten-player team roster and can be changed "
        "for current or past weeks."
    )
    scoring_weeks = [
        FANTASY_SEASON_START + timedelta(weeks=week_index)
        for week_index in range(FANTASY_SEASON_WEEKS)
    ]
    week_labels = [
        f"Week #{week_index + 1} ({week_start:%m/%d/%y}-{(week_start + timedelta(days=6)):%m/%d/%y})"
        for week_index, week_start in enumerate(scoring_weeks)
    ]
    current_week_index = min(
        max((date.today() - FANTASY_SEASON_START).days // 7, 0),
        FANTASY_SEASON_WEEKS - 1,
    )
    selected_week = st.selectbox(
        "Scoring week",
        week_labels,
        index=current_week_index,
        key="active_roster_week",
    )
    week_start = scoring_weeks[week_labels.index(selected_week)]
    week_end = week_start + timedelta(days=6)
    st.caption(f"Selected scoring week: {week_start:%b} {week_start.day} – {week_end:%b} {week_end.day}, {week_end.year}")

    teams = [dict(team) for team in database.get_teams()]
    if not teams:
        st.info("Add league members in League Setup first.")
    admin_unlocked = st.session_state.get("overview_admin_unlocked", False)
    if not admin_unlocked:
        st.caption("Unlock admin access on Overview to edit or submit weekly rosters.")

    slot_labels = {
        "C": "C",
        "F1": "F",
        "F2": "F",
        "G1": "G",
        "G2": "G",
    }

    for team in teams:
        team_id = team["id"]
        team_roster = [dict(player) for player in database.get_team_roster(team_id, week_start)]
        active_roster = [
            dict(player)
            for player in database.get_week_active_roster(team_id, week_start)
        ]
        current_by_slot = {player["slot"]: player for player in active_roster}

        with st.expander(team["name"], expanded=not active_roster):
            if len(team_roster) != 10:
                st.warning(
                    f"{team['name']} needs a complete ten-player roster for this week in League Setup before an active lineup can be submitted."
                )
                continue

            player_by_id = {player["player_id"]: player for player in team_roster}
            options = [None, *player_by_id.keys()]

            def player_label(player_id):
                if player_id is None:
                    return "Select a player"
                return player_by_id[player_id]["full_name"]

            with st.form(f"active_roster_form_{team_id}_{week_start.isoformat()}"):
                columns = st.columns(5)
                players_by_slot = {}
                for index, slot in enumerate(database.ACTIVE_ROSTER_SLOTS):
                    saved_id = current_by_slot.get(slot, {}).get("player_id")
                    default_index = options.index(saved_id) if saved_id in options else 0
                    with columns[index]:
                        players_by_slot[slot] = st.selectbox(
                            slot_labels[slot],
                            options,
                            index=default_index,
                            format_func=player_label,
                            key=f"active_{team_id}_{week_start.isoformat()}_{slot}",
                            disabled=not admin_unlocked,
                        )
                submit_roster = st.form_submit_button(
                    "Submit roster and recalculate points",
                    type="primary",
                    disabled=not admin_unlocked,
                )

            if submit_roster:
                if any(value is None for value in players_by_slot.values()):
                    st.error("Select a player in all five active roster slots.")
                elif len(set(players_by_slot.values())) != 5:
                    st.error("Select five different players for the active roster.")
                else:
                    try:
                        database.save_week_active_roster(
                            team_id, week_start, players_by_slot
                        )
                        selected_players = {
                            slot: player_by_id[player_id]
                            for slot, player_id in players_by_slot.items()
                        }
                        with st.spinner(
                            f"Refreshing player scores and recalculating {team['name']}'s total…"
                        ):
                            _, warnings = update_scores(
                                team_id, week_start, selected_players
                            )
                        st.success(
                            f"Saved {team['name']}'s active roster and recalculated this week's points."
                        )
                        render_score_warnings(warnings)
                    except ValueError as exc:
                        st.error(str(exc))

            scores = [
                dict(row)
                for row in database.get_week_player_scores(team_id, week_start)
            ]
            if active_roster:
                if scores:
                    team_total = sum(player["points"] for player in scores)
                    st.metric(f"{team['name']} weekly points", team_total)
                log_warnings = refresh_player_logs_for_week(team_roster, week_start)
                render_score_warnings(log_warnings)

                active_slot_by_player = {
                    player["player_id"]: slot_labels[player["slot"]]
                    for player in active_roster
                }
                season = season_for_week(week_start)
                roster_order = {
                    player["player_id"]: index
                    for index, player in enumerate(team_roster)
                }
                roster_display = sorted(
                    team_roster,
                    key=lambda player: (
                        {"C": 0, "F": 1, "G": 2}.get(
                            active_slot_by_player.get(player["player_id"], "BN"), 3
                        ),
                        roster_order[player["player_id"]],
                    ),
                )
                score_frame = pd.DataFrame(
                    [
                        {
                            "Position": active_slot_by_player.get(
                                player["player_id"], "BN"
                            ),
                            "Player": player["full_name"],
                            "NBA Team": player["nba_team_name"],
                            "Points": database.get_player_week_points(
                                player["player_id"], season, week_start
                            ),
                        }
                        for player in roster_display
                    ]
                )
                st.dataframe(
                    score_frame,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Points": st.column_config.NumberColumn(
                            format="%d", alignment="left"
                        )
                    },
                )

elif page == "Schedule Grid":
    from score_service import get_season_schedule, season_for_week

    st.title("NBA Schedule Grid")
    season = season_for_week(FANTASY_SEASON_START)
    st.write(
        f"Regular-season games per NBA team for each fantasy week in the {season} season. "
        "The schedule is bundled with the app and reused by the Weekly Scores tab."
    )
    try:
        with st.spinner("Loading the saved NBA schedule…"):
            schedule = get_season_schedule(season)
        if not schedule:
            st.warning("The NBA schedule did not return any regular-season games.")
        else:
            week_columns = [f"Week #{index + 1}" for index in range(FANTASY_SEASON_WEEKS)]
            teams_in_schedule = sorted(
                {game["nba_team_name"] for game in schedule}, key=str.casefold
            )
            schedule_grid = pd.DataFrame(
                0, index=teams_in_schedule, columns=week_columns, dtype="int64"
            )
            for game in schedule:
                game_date = date.fromisoformat(game["game_date"])
                week_index = (game_date - FANTASY_SEASON_START).days // 7
                if 0 <= week_index < FANTASY_SEASON_WEEKS:
                    week_column = week_columns[week_index]
                    schedule_grid.loc[game["nba_team_name"], week_column] += 1
            schedule_grid.index.name = "NBA Team"
            st.caption(f"{len(schedule)} team-game dates loaded from the saved schedule.")
            current_week_index = (monday_of(date.today()) - FANTASY_SEASON_START).days // 7
            display_grid = schedule_grid.reset_index()
            if 0 <= current_week_index < FANTASY_SEASON_WEEKS:
                current_week_column = week_columns[current_week_index]

                def highlight_current_week(data):
                    styles = pd.DataFrame("", index=data.index, columns=data.columns)
                    styles[current_week_column] = "background-color: #fff2cc; color: #172033"
                    return styles

                display_grid = display_grid.style.apply(highlight_current_week, axis=None)
                st.caption(f"Highlighted column: Week #{current_week_index + 1} (current week).")
            st.dataframe(
                display_grid,
                hide_index=True,
                column_config={
                    **numeric_column_config(schedule_grid.reset_index()),
                    "NBA Team": st.column_config.TextColumn(
                        width="medium", alignment="left"
                    ),
                },
                width="stretch",
                height=650,
            )
    except Exception as exc:
        st.error(f"Could not load the NBA schedule: {exc}")

elif page == "Weekly Scores":
    st.title("Weekly Scores")
    scoring_weeks = [
        FANTASY_SEASON_START + timedelta(weeks=week_index)
        for week_index in range(FANTASY_SEASON_WEEKS)
    ]
    week_labels = [
        f"Week #{index + 1} ({start:%m/%d/%y}-{(start + timedelta(days=6)):%m/%d/%y})"
        for index, start in enumerate(scoring_weeks)
    ]
    current_week_index = min(
        max((date.today() - FANTASY_SEASON_START).days // 7, 0),
        FANTASY_SEASON_WEEKS - 1,
    )
    week_col, team_col = st.columns(2)
    with week_col:
        selected_week = st.selectbox(
            "Scoring week", week_labels, index=current_week_index, key="scores_week"
        )
    week_start = scoring_weeks[week_labels.index(selected_week)]
    teams = [dict(team) for team in database.get_teams()]
    if not teams:
        st.info("Add league members in League Setup first.")
    else:
        with team_col:
            selected_team = st.selectbox(
                "Team", teams, format_func=lambda team: team["name"], key="scores_team"
            )
        render_weekly_scores(
            selected_team["id"], selected_team["name"], week_start
        )

else:
    st.title("Free Agents")
    st.write(
        "NBA scoring leaders who are not on a fantasy roster for the current week, "
        "ranked by points per game."
    )
    from free_agent_service import get_scoring_leaders
    from score_service import season_for_week

    current_week_start = monday_of(date.today())
    season = season_for_week(current_week_start)
    elapsed_weeks = min(
        max((current_week_start - FANTASY_SEASON_START).days // 7 + 1, 1),
        FANTASY_SEASON_WEEKS,
    )
    scoring_periods = ["Total"] + [
        f"Last {week_count} week" + ("s" if week_count != 1 else "")
        for week_count in range(1, elapsed_weeks + 1)
    ]
    selected_period = st.selectbox(
        "Scoring period", scoring_periods, key="free_agent_scoring_period"
    )
    period_start = None
    period_end = None
    if selected_period != "Total":
        selected_week_count = int(selected_period.split()[1])
        period_start = max(
            FANTASY_SEASON_START,
            current_week_start - timedelta(weeks=selected_week_count - 1),
        )
        season_end = FANTASY_SEASON_START + timedelta(weeks=FANTASY_SEASON_WEEKS) - timedelta(days=1)
        period_end = min(date.today(), season_end)
    refresh_leaders = st.button("Refresh scoring leaders", key="refresh_free_agents")
    try:
        with st.spinner("Loading NBA scoring leaders…"):
            leaders, stats_warning = get_scoring_leaders(
                season,
                refresh=refresh_leaders,
                period_start=period_start,
                period_end=period_end,
            )
        if stats_warning:
            st.warning(stats_warning)

        rostered_player_ids = {
            player["player_id"]
            for team in database.get_teams()
            for player in database.get_team_roster(team["id"], current_week_start)
        }
        free_agents = [
            player for player in leaders
            if player["player_id"] not in rostered_player_ids
        ][:30]
        if free_agents:
            free_agent_frame = pd.DataFrame(free_agents).rename(
                columns={
                    "full_name": "Player",
                    "position": "Position",
                    "nba_team_name": "NBA Team",
                    "points_per_game": "Points per Game",
                }
            )[["Player", "Position", "NBA Team", "Points per Game"]]
            st.dataframe(
                free_agent_frame,
                hide_index=True,
                width="stretch",
                column_config={
                    "Points per Game": st.column_config.NumberColumn(
                        format="%.1f", alignment="left"
                    ),
                },
            )
        elif not stats_warning:
            st.info("No unrostered players with regular-season appearances were found.")
    except Exception as exc:
        st.error(f"Could not load free agents: {exc}")
