# Status tags & upcoming (replacing TSSK and UMTK)

These two services replace the Plex-only tools *TV Show Status for Kometa* (TSSK) and
*Upcoming Movies & TV Shows for Kometa* (UMTK). They write **tags** on Jellyfin items; an overlay
renderer such as [JellyTag](https://github.com/l3akage/jellyfin-plugins) turns the tags into
banners (its *Status Banner* panel and rules).

## Status tags (`status_tags:` in config.yml)

```yaml
status_tags:
  enabled: true
  library: Serien                 # default: the first series library
  recent_days_new_show: 7
  future_days_new_season: 31
  recent_days_new_season_started: 7
  future_days_upcoming_episode: 31
  future_days_upcoming_finale: 31
  recent_days_season_finale: 7
  recent_days_final_episode: 7
  ignore_finales_tag: ignorefinales   # series with this tag never get finale tags
  # process_new_shows, process_new_season_soon, ... : false disables a tag
```

All data comes from TMDb (status, last/next episode); Sonarr is not required. Written tags:

| Tag | Meaning |
|-----|---------|
| `NewShow` | added to the library within `recent_days_new_show` days (not for `Coming Soon` items); with `new_show_premiered_within_days: N` it must also have premiered within N days (useful after importing a whole library) |
| `NewSeasonSoon:<season>:<date>` | next episode is episode 1 of season 2+, within `future_days_new_season` |
| `NewSeasonStarted:<season>` | season 2+ premiere aired within `recent_days_new_season_started` |
| `UpcomingEpisode:<date>` | next episode within `future_days_upcoming_episode` |
| `UpcomingFinale:<date>` | final episode of an ended/canceled series within `future_days_upcoming_finale` |
| `SeasonFinale:<season>` | season finale aired within `recent_days_season_finale` |
| `FinalEpisode` | series finale aired within `recent_days_final_episode` |
| `ReturningShows` / `EndedShows` / `CanceledShows` | TMDb status (`ReturningShows` only without an upcoming tag) |

Only tags with these names are ever added or removed; other tags of an item are never touched.

## Upcoming (`upcoming:` in config.yml)

Disabled by default because it writes files.

```yaml
upcoming:
  enabled: true
  # placeholder_video: /config/placeholder.mp4   # otherwise generated with ffmpeg (included in the Docker image)
  cleanup: true
  movies:
    library: Filme
    placeholder_root: /umtkmovies      # must be part of the Jellyfin library and writable for JFC
    future_days: 30
    past_days: 14
    include_in_cinemas: false
    exclude_tags: [exclude, private]
  tv:
    library: Serien
    placeholder_root: /umtktv
    future_days: 30
    past_days: 14
```

Requires Radarr/Sonarr API keys. Titles that are monitored, have no file yet and release within the
window get a placeholder folder `Title (Year) [tmdbid-N]` (shows: `[tvdbid-N]`, with `Season 01`), and the tags
`Coming Soon` plus `Expected:<date>` (movies) / `Premieres:<date>` (shows) for future releases.
Placeholders are deleted again when the file arrives or the title leaves the window.
**Only folders containing the marker file `.jfc-placeholder` are ever deleted.**

Jellyfin is asked to scan its libraries after placeholders were created or removed; the tags are
applied as soon as the items exist (usually on the next run).
