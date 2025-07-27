# Navarro-PW: OSINT Username Checker

<p align='center'><img src='https://github.com/user-attachments/assets/ed5f2eef-a9cc-44cd-8745-59363d722417' alt='Navarro_VieDeMaMere' width='60%'></p>

A Python tool that checks for the existence of a username across 20+ social media and web platforms using Playwright (forked from [Navarro](https://github.com/noobosaurus-r3x/Navarro) by [noobosaurus-r3x](https://github.com/noobosaurus-r3x)).

## What's New? 

- Screenshot capture and OCR to overcome limitations with Twitch and Discord.

---

## Features

- Checks username availability on major platforms (GitHub, Reddit, Instagram, etc.)
- Smart rate limiting with persistent storage between runs
- User agent rotation to reduce detection
- Batch checking via file input
- JSON export for integration with other tools
- Session management for improved performance
- No API keys required

## Requirements

- Python 3.6+
- `requests`
- `rich` 
- `playwright`
- `pytesseract`
- `Pillow`

or 

```bash
pip install -r requirements.txt
```

## Installation
### From source

```bash
# Clone the repository
git clone https://github.com/jestlandia/Navarro-pw.git
cd Navarro-pw

# Install dependencies
brew install tesseract tesseract-lang
pip install requests rich Pillow pytesseract
```

### Docker
```bash
docker build -t navarro .
docker run -it navarro <command>
```

## Usage

### Check single username
```bash
python3 navarro_pw.py username
```

### Check multiple usernames from file
```bash
python3 navarro_pw.py --list usernames.txt
```

### Export results to JSON
```bash
python3 navarro_pw.py username --export results.json
```

### Combine options
```bash
python3 navarro_pw.py --list users.txt --export output.json
```

## Supported Platforms

- GitHub
- GitLab
- Reddit
- Instagram
- Facebook
- TikTok
- LinkedIn
- Pinterest
- Pastebin
- Telegram
- Snapchat
- Strava
- Threads
- Mastodon
- Bluesky
- Spotify
- SoundCloud
- YouTube
- Medium
- Chess.com
- Keybase
- Linktree
- VK
- Steam
- DeviantArt
- Vimeo
- Twitch
- Discord
- RuTube

**Note**: X/Twitter is not supported due to lack of reliable detection methods.

## Output

The tool provides results in three categories:
- ✅ **Found** - Profile exists
- ❌ **Not Found** - Profile doesn't exist
- ⚠️ **Error** - Network issues, rate limiting, or timeouts

## Rate Limiting

The tool implements smart rate limiting to respect platform limits:
- Adaptive delays between requests
- Persistent storage of rate limits between runs
- Automatic retry with exponential backoff
- Per-platform tracking

Rate limit data is stored in `~/.navarro_rate_limits.pkl`

## JSON Export Format

```json
{
  "username": {
    "timestamp": "2024-01-01T12:00:00",
    "stats": {
      "found": 12,
      "not_found": 14,
      "network_error": 0,
      "rate_limited": 0,
      "timeout": 0,
      "unknown_error": 0
    },
    "results": {
      "GitHub": "found",
      "GitLab": "not_found",
      ...
    },
    "found_profiles": {
      "GitHub": "https://github.com/username",
      "Reddit": "https://reddit.com/user/username",
      ...
    }
  }
}
```

## Limitations

- Results may include false positives/negatives
- Some platforms may block automated checking
- Rate limiting may slow down large batch checks
- No proxy support currently implemented
- Single-threaded to avoid overwhelming target platforms

## Privacy & Legal

- This tool only checks public information
- Respect platform terms of service
- Use responsibly and ethically
- The authors are not responsible for misuse

## Contributing

Contributions are welcome! Please feel free to fork it too.

## License

MIT License - see LICENSE file for details
