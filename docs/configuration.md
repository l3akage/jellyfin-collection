# Configuration Guide

Complete reference for all JFC configuration options.

## Table of Contents

- [Configuration System](#configuration-system)
- [config.yml Reference](#configyml-reference)
- [Secrets (.env)](#secrets-env)
- [Kometa YAML Configuration](#kometa-yaml-configuration)
- [Collection Builders](#collection-builders)
- [Filters](#filters)
- [Scheduling](#scheduling)

## Configuration System

JFC uses a **dual configuration system** for flexibility and security:

### Priority Order (highest first)

1. **Environment variables** - Override everything
2. **`.env` file** - Secrets and local overrides
3. **`config.yml` settings** - Main configuration
4. **Default values** - Fallback

### Benefits

- **Portable** - Share `config.yml` without exposing secrets
- **Versionable** - Commit configuration to Git
- **Docker-friendly** - Minimal environment variables needed
- **Backward compatible** - Existing `.env` configs still work

## config.yml Reference

The `settings` section in `config/config.yml` contains all non-secret configuration:

```yaml
settings:
  # ---------------------------------------------------------------------------
  # JELLYFIN
  # ---------------------------------------------------------------------------
  jellyfin:
    url: http://jellyfin:8096
    # api_key: in .env (secret)

  # ---------------------------------------------------------------------------
  # TMDB
  # ---------------------------------------------------------------------------
  tmdb:
    language: fr          # ISO 639-1 language code
    region: FR            # ISO 3166-1 country code
    # api_key: in .env (secret)

  # ---------------------------------------------------------------------------
  # OPENAI (AI Poster Generation)
  # ---------------------------------------------------------------------------
  openai:
    enabled: true                 # Enable AI posters
    explicit_refs: false          # Include titles in visual signatures
    force_regenerate: false       # Regenerate all posters on every run
    missing_only: true            # Default for CLI 'regenerate-posters' command
    poster_history_limit: 5       # Keep N old posters (0=unlimited)
    prompt_history_limit: 10      # Keep N prompt files (0=unlimited)
    poster_logo_text: "NETFLEX"   # Logo text on generated posters
    # api_key: in .env (secret)
    # Note: Scheduled poster job always forces regeneration regardless of missing_only

  # ---------------------------------------------------------------------------
  # RADARR (Movies)
  # ---------------------------------------------------------------------------
  radarr:
    url: http://radarr:7878
    root_folder: /movies
    quality_profile: HD-1080p
    default_tag: jfc
    only_released: false   # true = only request movies with a digital/physical release (skip theatrical-only)
    # api_key: in .env (secret)

  # ---------------------------------------------------------------------------
  # SONARR (Series)
  # ---------------------------------------------------------------------------
  sonarr:
    url: http://sonarr:8989
    root_folder: /tv
    quality_profile: HD-1080p
    default_tag: jfc
    # api_key: in .env (secret)

  # ---------------------------------------------------------------------------
  # DISCORD (Webhooks)
  # ---------------------------------------------------------------------------
  discord:
    webhook_url: ""              # Main webhook
    webhook_error: ""            # Errors (falls back to main)
    webhook_run_start: ""        # Run start notifications
    webhook_run_end: ""          # Run end notifications
    webhook_changes: ""          # Collection changes

  # ---------------------------------------------------------------------------
  # SCHEDULER
  # ---------------------------------------------------------------------------
  scheduler:
    collections_cron: "0 3 * * *"    # Daily at 3am
    posters_cron: "0 4 1 * *"        # 1st of month at 4am
    run_on_start: true               # Sync on container start
    run_all_on_start: false          # Run all collections on startup
    ignore_collection_schedule: false # Ignore per-collection schedule on cron runs
    timezone: Europe/Paris           # Timezone for cron

  # ---------------------------------------------------------------------------
  # APPLICATION
  # ---------------------------------------------------------------------------
  log_level: INFO                    # DEBUG, INFO, WARNING, ERROR
  matcher_preload_limit: 50000       # Max preloaded items per library
  dry_run: false                     # Preview mode (no changes)
```

## Secrets (.env)

Only API keys and secrets should be in `.env`:

```bash
# =============================================================================
# SECRETS ONLY - Configuration is in config.yml
# =============================================================================

# Required
JELLYFIN_API_KEY=your_jellyfin_api_key
TMDB_API_KEY=your_tmdb_api_key

# Optional - Trakt
TRAKT_CLIENT_ID=
TRAKT_CLIENT_SECRET=

# Optional - Radarr/Sonarr
RADARR_API_KEY=
SONARR_API_KEY=

# Optional - OpenAI
OPENAI_API_KEY=

# Optional - Overrides (uncomment to override config.yml)
# OPENAI_ENABLED=false
# SCHEDULER_TIMEZONE=UTC
```

### Trakt Authentication

Trakt tokens are managed automatically:

```bash
# Authenticate (interactive)
jfc trakt-auth

# Check status
jfc trakt-status

# Logout
jfc trakt-logout
```

Tokens are stored in `/data/trakt_tokens.json` and refreshed automatically.

## Trakt sign-in

Trakt no longer issues a client secret to new apps; they sign users in with
[PKCE](https://developer.trakt.tv/docs/pkce). Without `TRAKT_CLIENT_SECRET`, JFC does the same:

```bash
jfc trakt-auth
```

Trakt developer apps only accept `localhost` redirect URIs. Enter `http://localhost:8765/callback`
as *Redirect URI* in your Trakt app (or set `TRAKT_REDIRECT_URI` to the one you registered). The
command prints an authorize URL; after you allow access, the browser is redirected to that
`localhost` address, JFC listens there and picks up the code itself.

If the browser runs on another machine than JFC (server, Docker), the redirect cannot arrive. The
browser then shows a "can't connect" page, but its address bar contains `...?code=...`: run
`jfc trakt-auth --paste` and paste that whole URL (or just the code). The tokens are stored in the
data directory and refreshed automatically (without a secret). Apps that still have a secret keep
using the device code flow (`--pkce` forces PKCE, `--device` forces the device flow). The sign-in is
interactive, so run it once by hand (in Docker:
`docker compose exec -it jellyfin-collection jfc trakt-auth --paste`; the listener would need the
port published).

## Kometa YAML Configuration

JFC uses the same YAML format as Kometa/Plex Meta Manager.

### Basic Structure

```yaml
# config.yml
libraries:
  Films:                          # Must match Jellyfin library name
    collection_files:
      - file: Films.yml           # Path to collection definitions

  Séries:
    collection_files:
      - file: Series.yml
    # Library-specific Sonarr overrides
    sonarr:
      tag: sonarr-series
      root_folder_path: /tv
```

### Collection File Example

```yaml
# Films.yml
templates:
  movie_template:
    sync_mode: sync
    schedule: daily
    sort_title: "!<<collection_name>>"

collections:
  "Trending Movies":
    template: {name: movie_template}
    tmdb_trending_weekly: 20
    summary: "Movies trending this week on TMDb"

  "Popular Movies":
    template: {name: movie_template}
    tmdb_popular: 50
    filters:
      year.gte: 2020
```

## Collection Builders

### TMDb Builders

#### `tmdb_trending_weekly` / `tmdb_trending_daily`

```yaml
collections:
  "Trending":
    tmdb_trending_weekly: 20    # Number of items
```

#### `tmdb_popular`

```yaml
collections:
  "Popular Movies":
    tmdb_popular: 50
```

#### `tmdb_discover`

Full TMDb discover API support:

```yaml
collections:
  "French Comedies 2024":
    tmdb_discover:
      with_genres: 35                    # Comedy
      with_original_language: fr
      primary_release_year: 2024
      sort_by: popularity.desc
      limit: 30
```

Available discover parameters:
- `with_genres` - Genre IDs (comma-separated)
- `with_original_language` - ISO 639-1 language code
- `primary_release_year` - Exact year
- `primary_release_date.gte/lte` - Date range
- `vote_average.gte/lte` - Rating range
- `with_runtime.gte/lte` - Runtime in minutes
- `sort_by` - Sort order
- `limit` - Max results

#### `tmdb_list`

```yaml
collections:
  "TMDb List":
    tmdb_list:
      - 710
      - https://www.themoviedb.org/list/710
```

### Trakt Builders

#### `trakt_trending` / `trakt_popular`

```yaml
collections:
  "Trakt Trending":
    trakt_trending: 20
```

#### `trakt_chart`

```yaml
collections:
  "Most Watched This Week":
    trakt_chart:
      chart: watched
      time_period: weekly
      limit: 25
```

Chart types: `watched`, `trending`, `popular`, `recommended`

### IMDb Builders

#### `imdb_chart`

```yaml
collections:
  "IMDb Top 250":
    imdb_chart:
      list_ids:
        - top
      limit: 250
```

Supported chart IDs: `top`, `boxoffice`, `moviemeter`, `tvmeter`

#### `imdb_list`

```yaml
collections:
  "IMDb List":
    imdb_list:
      list_ids:
        - ls055592025
```

### Arr Taglist Builders

#### `radarr_taglist` (Movies)

```yaml
collections:
  "Best Motion Picture":
    radarr_taglist:
      tags:
        - best-motion-picture
      limit: 100
```

#### `sonarr_taglist` (Shows)

```yaml
collections:
  "Backlog Series":
    sonarr_taglist:
      tags:
        - backlog
```

### Library Search

`plex_search` is supported for Jellyfin library filtering.

```yaml
collections:
  "Recent Comedy Movies":
    plex_search:
      all:
        Genres: Comedy
        year.gte: 2012
      limit: 50
```

Supported `plex_search` keys:
- `all.Genres` or `all.genre` (string or list)
- `all.year`
- `all.year.gte`
- `all.year.lte`
- top-level `limit`

## Filters

Apply filters after fetching from source:

```yaml
collections:
  "Recent Trending":
    tmdb_trending_weekly: 50
    filters:
      year.gte: 2020
      vote_average.gte: 6.0
```

### Available Filters

| Filter | Description | Example |
|--------|-------------|---------|
| `year.gte` | Minimum year | `2020` |
| `year.lte` | Maximum year | `2024` |
| `year` | Exact year | `2023` |
| `vote_average.gte` | Minimum rating | `7.0` |
| `vote_average.lte` | Maximum rating | `9.0` |
| `original_language` | Language code | `en` |
| `genre` | Genre name | `Action` |
| `genre.not` | Exclude genre | `Horror` |

## Scheduling

### Schedule Types

```yaml
collections:
  "Daily Updated":
    schedule: daily
    tmdb_trending_daily: 20

  "Weekly Updated":
    schedule: weekly(sunday)
    tmdb_trending_weekly: 30

  "Monthly Updated":
    schedule: monthly(1)
    tmdb_popular: 100

  "Never Auto-Update":
    schedule: never
    # Manual updates only
```

### Cron Expressions

For the scheduler daemon (in `config.yml`):

| Expression | Meaning |
|------------|---------|
| `0 3 * * *` | Daily at 3:00 AM |
| `0 17 * * *` | Daily at 5:00 PM |
| `0 */6 * * *` | Every 6 hours |
| `0 4 1 * *` | 1st of month at 4:00 AM |
| `0 3 * * 0` | Every Sunday at 3:00 AM |

Startup behavior flags:

- `run_on_start` - Run a sync when the scheduler starts.
- `run_all_on_start` - On startup run, process all collections regardless of per-collection schedule.
- `ignore_collection_schedule` - For regular scheduled cron runs, ignore per-collection schedule rules.

## Data Directories

```
/config/                    # Configuration (read-only in Docker)
├── config.yml              # Main config + settings
├── Films.yml               # Movie collections
└── Series.yml              # Series collections

/data/                      # Generated data
├── posters/                # AI-generated posters
│   └── Films/
│       └── Trending/
│           ├── poster.png
│           ├── history/
│           └── prompts/
├── cache/                  # API cache
│   └── visual_signatures_cache.json
├── trakt_tokens.json       # Trakt OAuth tokens
└── reports/                # Run reports
    └── report_xxx.md

/logs/                      # Application logs
├── jfc.log
└── error.log
```

## Next Steps

- [Kometa Migration](kometa-migration.md) - Migrate existing configs
- [AI Poster Generation](ai-posters.md) - Setup AI posters

## Safety limits

`settings.max_removal_ratio` (default `0.5`): a sync never removes more than this share of a
collection's items in one run (only applies from 5 removals on). A larger removal is skipped and
logged, because it almost always means the Jellyfin library listing was incomplete. Set it to `1`
to disable the guard.

## Collection groups

Jellyfin has no folders for collections. JFC groups them instead:

```yaml
collection_groups: [IMDb, Universen, Sammlungen, Filmreihen, Streaming, Saisonal]   # order in the Collections tab

libraries:
  Filme:
    collection_files:
      - default: imdb                  # group "IMDb" (built-in defaults: IMDb, Universen, Streaming, Netzwerke)
      - file: config/Movie Collections.yml
        group: Sammlungen              # group of every collection of this file ...
```

A collection can set its own `group:` (it wins over the entry's). Collections with a
`range(...)` schedule default to `Saisonal`, dynamic TMDb franchises to `Filmreihen`.

Every grouped collection gets the group as sort title prefix (`00 IMDb - Top 250`, unless the
collection sets `sort_title`) and as Jellyfin genre, so the Collections tab sorts by group and
can be filtered by it.
