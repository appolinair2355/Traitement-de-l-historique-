# VIP KOUAMÉ Predictions Bot

A Telegram bot for Baccarat game analysis and predictions using Telethon. Scrapes predictions from Telegram channels and analyzes games using a gap-based prediction algorithm. Multi-admin management with granular permissions. Inline keyboard menu system organized in French. Deployment package named "molirokkk".

## Project Structure

- `main.py` - Entry point: starts the health-check web server (port 5000) and the Telegram bot
- `config.py` - Configuration (bot token, admin ID, Telethon API credentials)
- `bot_handler.py` - All Telegram bot command handlers (51 handlers in Handlers class)
- `auth_manager.py` - Telethon authentication management (phone login flow)
- `scraper.py` - Telethon-based scraper that reads messages from Telegram channels
- `storage.py` - JSON file-based storage for predictions and game data (29 commands in ALL_COMMANDS)
- `game_analyzer.py` - Baccarat game parsing and category stats (victoire, parité, structure, costumes, face cards, face+suit)
- `predictor.py` - Gap-based prediction engine (67 categories total including 32 face+suit combos)
- `pdf_generator.py` - PDF report generation using ReportLab
- `pdf_analyzer.py` - PDF analysis for extracting number+costume pairs

## Tech Stack

- **Language**: Python 3.12
- **Telegram Bot**: python-telegram-bot (v22)
- **Telegram Scraper**: Telethon (MTProto client)
- **Web Server**: aiohttp (health endpoint at `/` and `/health`)
- **PDF**: ReportLab, pdfplumber
- **Data Storage**: JSON files in `data/` directory

## Menu Organization (Inline Keyboard)

The bot menu is organized into 7 sections:
1. **Recherche** — Search commands (hsearch, searchcard, search)
2. **Prédiction** — Prediction commands (gload, gpredict, gpredictload, ganalyze, predictsetup)
3. **Analyse** — Analysis commands (gstats, gvictoire, gparite, gstructure, gplusmoins, gcostume, gvaleur, gecartmax, gclear)
4. **Cycles** — Costume cycle correction (gcycle, gcycleauto) — generates complete number [costume] correction lists
5. **Canaux** — Channel management (addchannel, helpcl, channels, usechannel, removechannel)
6. **Documentation** — Help and docs
7. **Administration** — Admin management (main admin only)

## Key Features

- **67 predictor categories**: victoire (3), parité (2), structure (4), 2K/3K (4), plus/moins (6), costumes (8), face cards (8), face+suit (32)
- **Costume cycle correction**: `/gcycle` tests predefined cycles, `/gcycleauto` finds optimal cycle+filter combos automatically. Both generate complete `numéro [costume]` correction lists (like PDF analysis format)
- **14 number filters** for cycle auto-discovery (pairs, impairs, ×10, ×5, digit endings, etc.)
- **Multi-admin**: granular per-command permissions, main admin + sub-admins

## Workflow

- **Start application**: `python main.py` on port 5000

## Deployment

- `render.yaml` for Render.com deployment
- `molirokkk.zip` — deployment package (13 files, ~57 KB)
