# Migrating from Kometa / Plex Meta Manager

This guide helps you migrate your existing Kometa or Plex Meta Manager configuration to JFC.

## Overview

JFC is designed to be largely compatible with Kometa YAML configurations. Most configurations will work with minimal changes.

## What works like in Kometa

| Kometa feature | JFC |
|----------------|-----|
| Templates with `<<variables>>`, `optional`, `default`, template lists | ✅ |
| `template_variables:` on `- file:` entries | ✅ |
| `- default: imdb / network / streaming / universe` | ✅ built in (see below) |
| `schedule: range(MM/DD-MM/DD)` + `delete_not_scheduled` (seasonal) | ✅ |
| `dynamic:` movable holidays (`easter_dates`, `good_friday_dates`, `chinese_new_year_dates`) | ✅ |
| `item_label`, `non_item_remove_label`, `item_label.remove` | ✅ as Jellyfin tags |
| `build_collection: false` (label only) | ✅ |
| `minimum_items` / `delete_below_minimum` | ✅ |
| `radarr_add_missing` / `sonarr_add_missing`, library `add_missing` | ✅ (see warning below) |
| `url_poster`, `smart_label`, `visible_*`, `collection_mode`, overlays, operations | ❌ ignored |

**Kometa defaults.** `imdb` (Top 250 + Popular), `network` (TMDb network IDs), `streaming`
(TMDb watch providers; the region is `settings.tmdb.region`) and `universe` (`mcu`, `trek`, `star`,
`wizard`). `include:` / `exclude:` / `name_format:` / `name_<key>:` / `sync_mode` / `sort_by` are
honored. An empty `include:` for `network` builds nothing. `watch_region` (streaming) overrides the
TMDb region.

**Movies and series in one collection.** Jellyfin collections can hold both. Set
`include_series: <series library name>` on a movie collection (or `series_library: Serien` as
`template_variables` of `- default: universe`): the same sources are queried for series (`tmdb_show`,
`tmdb_keyword`, Trakt/MDBList series entries) and matched in that library. The Star Trek and Star Wars
universes ship with their series; the MCU uses its TMDb keyword for both.

**add_missing.** `settings.radarr.add_missing` / `settings.sonarr.add_missing` default to `true`
(the previous JFC behavior). Kometa's default is `false`: set it explicitly to avoid that seasonal
and franchise collections flood Radarr/Sonarr.

### Library filters

`plex_all: true` with `filters:` and `plex_search` (`all` / `any` / `not`, `limit`, `sort_by`) are
evaluated against Jellyfin items. Attributes: `title`, `genre`, `year`, `release`, `added` (days),
`label`, `edition` (`{edition-...}` in the file name), `resolution`, `has_dolby_vision`, `hdr`,
`audio_language`, `subtitle_language`, `studio`, `network`, `audience_rating`, `critic_rating`,
`runtime`, `content_rating`, `filepath`, `tmdb_status`. Modifiers: `.not`, `.is`, `.isnot`, `.gt`,
`.gte`, `.lt`, `.lte`, `.regex`, `.begins`, `.ends`, `.before`, `.after`. Comma separated values
mean "any of". Unknown attributes are ignored with a debug message.

## Migration Steps

### 1. Copy Configuration Files

Copy your Kometa configuration files to the JFC config directory:

```bash
# Copy main config
cp /path/to/kometa/config/config.yml ./config/

# Copy collection files
cp /path/to/kometa/config/*.yml ./config/
```

### 2. Update Library Names

Update library names in `config.yml` to match your **Jellyfin** library names exactly:

```yaml
# Before (Plex library names)
libraries:
  Movies:
    collection_files:
      - file: config/Movies.yml

# After (Jellyfin library names)
libraries:
  Films:                            # Must match Jellyfin exactly
    collection_files:
      - file: config/Films.yml
```

### 3. Remove Plex-Specific Settings

Remove settings that are specific to Plex:

```yaml
# Remove these sections
plex:
  url: ...
  token: ...

# Remove Plex-specific collection settings
collections:
  "My Collection":
    # Remove:
    # - plex_all
    # - plex_pilots
    # - plex_collectionless
    # - smart_filter
    # - smart_url
```

### 4. Update Builders

Most builders are supported. Here's the mapping:

| Kometa Builder | JFC Support | Notes |
|----------------|-------------|-------|
| `tmdb_trending_weekly` | ✅ Supported | - |
| `tmdb_trending_daily` | ✅ Supported | - |
| `tmdb_popular` | ✅ Supported | - |
| `tmdb_discover` | ✅ Supported | - |
| `tmdb_now_playing` | ✅ Supported | - |
| `trakt_trending` | ✅ Supported | - |
| `trakt_popular` | ✅ Supported | - |
| `trakt_chart` | ✅ Supported | - |
| `trakt_list` | ✅ Supported | - |
| `plex_search` / `plex_all` + `filters` | ✅ Supported | Jellyfin library query, see [Library filters](#library-filters) |
| `tmdb_movie` / `tmdb_show` / `tvdb_show` | ✅ Supported | ID lists |
| `tmdb_collection` / `tmdb_keyword` / `tmdb_director` | ✅ Supported | Franchises, keywords, directors |
| `dynamic_collections` (`type: tmdb_collection`) | ✅ Supported | One collection per franchise in the library (Jellyfin `TmdbCollection` id) |
| `mdblist_list` | ✅ Supported | Public list URLs (API key optional) |
| `imdb_chart` | ✅ Supported | top, boxoffice, moviemeter, tvmeter |
| `imdb_list` | ✅ Supported | `ls...` lists |
| `tmdb_list` | ✅ Supported | ID/string/URL accepted |
| `radarr_taglist` | ✅ Supported | Build movie collections from Radarr tags |
| `sonarr_taglist` | ✅ Supported | Build series collections from Sonarr tags |
| `letterboxd_list` | ❌ Not supported | - |
| `reciperr_list` | ❌ Not supported | - |

### 5. Update Templates

Templates work the same way, but remove Plex-specific options:

```yaml
# Before
templates:
  movie:
    sync_mode: sync
    collection_mode: hide
    visible_library: false
    visible_home: false
    visible_shared: false

# After
templates:
  movie:
    sync_mode: sync
    # collection_mode, visible_* not applicable in Jellyfin
```

### 6. Validate Configuration

Run validation to check for issues:

```bash
jfc validate --config ./config
```

### 7. Test with Dry Run

Preview what will happen without making changes:

```bash
jfc run --config ./config --dry-run
```

## Common Changes

### Overlays

JFC itself does not draw overlays. Remove `overlay_files:` from the config; the equivalent is done by
the [JellyTag](https://github.com/l3akage/jellyfin-plugins) plugin (rating, resolution, audio language,
edition and status banners). Status and "coming soon" banners are driven by tags that JFC writes, see
[Status tags & upcoming](status-and-upcoming.md).

### Collection Mode

Jellyfin doesn't have the same collection visibility options. Remove:

```yaml
# Remove from collections
collection_mode: hide
visible_library: false
visible_home: false
visible_shared: false
```

### Smart Collections

`smart_filter` / `smart_url` are Plex-specific. Use `plex_all: true` + `filters:` or `plex_search`
(both are evaluated by JFC against the Jellyfin library) instead. Example of a conversion:

```yaml
# Before (Plex smart collection)
collections:
  "4K Movies":
    smart_filter:
      all:
        resolution: 4k

# After (JFC with a supported source builder)
collections:
  "4K Movies":
    tmdb_discover:
      sort_by: popularity.desc
      limit: 50
```

### Metadata

JFC focuses on collections, not metadata management. Remove:

```yaml
# Remove
metadata_files:
  - file: config/metadata.yml
```

## Example Migration

### Original Kometa Config

```yaml
# config.yml (Kometa)
libraries:
  Movies:
    collection_files:
      - file: config/Movies.yml
    overlay_files:
      - file: config/Overlays.yml
    metadata_files:
      - file: config/Metadata.yml

plex:
  url: http://plex:32400
  token: xxxx

tmdb:
  apikey: xxxx
  language: en
```

### Migrated JFC Config

```yaml
# config.yml (JFC)
libraries:
  Films:                          # Jellyfin library name
    collection_files:
      - file: config/Films.yml
  # Note: overlays and metadata removed
```

### Original Collection File

```yaml
# Movies.yml (Kometa)
templates:
  movie:
    sync_mode: sync
    collection_mode: hide
    visible_library: false

collections:
  "Trending Movies":
    template: {name: movie}
    tmdb_trending_weekly: 20
    summary: "Trending this week"
```

### Migrated Collection File

```yaml
# Films.yml (JFC)
templates:
  movie:
    sync_mode: sync
    # Removed Plex-specific options

collections:
  "Trending Movies":
    template: {name: movie}
    tmdb_trending_weekly: 20
    summary: "Trending this week"
```

## Environment Variables

Create a `.env` file with your API keys:

```bash
# Required
JELLYFIN_URL=http://jellyfin:8096
JELLYFIN_API_KEY=your_jellyfin_key
TMDB_API_KEY=your_tmdb_key        # Same as Kometa

# Optional (same as Kometa)
TRAKT_CLIENT_ID=your_trakt_id
TRAKT_CLIENT_SECRET=your_trakt_secret
RADARR_URL=http://radarr:7878
RADARR_API_KEY=your_radarr_key
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=your_sonarr_key
```

## Troubleshooting

### "Library not found in Jellyfin"

Library names must match exactly (case-sensitive). Check:
1. Jellyfin web UI for exact library name
2. Accented characters (e.g., "Séries" not "Series")

### "Builder not supported"

Check the supported builders list above. Some Plex-specific builders don't have equivalents.

### Collections not updating

1. Check `schedule` setting in collection config
2. Verify `sync_mode: sync` is set
3. Run with `--dry-run` to see what would change

## Next Steps

After migration:
- [Configuration Guide](configuration.md) - Full reference
- [AI Poster Generation](ai-posters.md) - Add AI posters
