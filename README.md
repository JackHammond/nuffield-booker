# Nuffield Health Gym Class Booker

Automated script to book gym classes at Nuffield Health gyms.

## Features

- Automatically logs into Nuffield Health account
- Books specified gym classes on the last available date
- Joins waitlists when classes are full
- Retries on timeouts
- Runs on schedule via GitHub Actions

## Setup

### Local Usage

1. Install dependencies:
```bash
pip install selenium webdriver-manager
```

2. Update configuration in `nuffieldbooker.py`:
```python
EMAIL = "your_email@example.com"
PASSWORD = "your_password"
TARGET_CLASSES = [
    "Reformer Pilates",
    "BoxFit",
    "Group Cycle"
]
```

3. Run the script:
```bash
python nuffieldbooker.py
```

### GitHub Actions (Automated)

1. **Add GitHub Secrets:**
   - Go to your repository → Settings → Secrets and variables → Actions
   - Add two secrets:
     - `NUFFIELD_EMAIL`: Your Nuffield Health email
     - `NUFFIELD_PASSWORD`: Your Nuffield Health password

2. **Configure Schedule:**
   - Edit `.github/workflows/nuffield-booker.yml`
   - Adjust the cron schedule (currently set to 7:00 AM UTC):
   ```yaml
   schedule:
     - cron: '0 7 * * *'  # 7 AM UTC
   ```
   - For UK time zones:
     - Use `0 7 * * *` for 7 AM UTC (8 AM BST / 7 AM GMT)
     - Use `0 6 * * *` for 6 AM UTC (7 AM BST / 6 AM GMT)

3. **Manual Trigger:**
   - Go to Actions tab → Nuffield Booker → Run workflow

## Configuration

Edit `nuffieldbooker.py` to customize:

- `TARGET_CLASSES`: List of class names to book
- `BOOKING_TIMEOUT_SECONDS`: How long to keep trying (default: 120 seconds)
- `TARGET_URL`: Your gym's timetable URL

## How It Works

1. Logs into your Nuffield Health account
2. Navigates to the gym timetable
3. Selects the last available date
4. Scans for matching classes from your target list
5. Books available classes or joins waitlists
6. Retries on timeouts (up to 2 attempts per class)
7. Reports success/failure for each class

## Troubleshooting

- **Classes not found**: Ensure class names in `TARGET_CLASSES` match exactly (case-insensitive)
- **Login fails**: Check your credentials in GitHub Secrets
- **Timeout errors**: Script automatically retries once before moving on

## License

MIT License
