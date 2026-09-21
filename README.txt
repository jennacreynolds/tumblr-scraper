▖▖▄▖▄▖▄▖▖ ▄▖▖ ▖▄▖▄▖
▌▌▐ ▌ ▐ ▌ ▌▌▛▖▌▌ ▙▖
▚▘▟▖▙▌▟▖▙▖▛▌▌▝▌▙▖▙▖
                   

TUMBLR SCRAPER

This makes a local backup of a public Tumblr blog.

It does not need a Tumblr login, account, API key, or upload to a server.

ANDROID WITH PYDROID 3

1. Install Pydroid 3 from Google Play.
2. Download Tumblr-Scraper.zip.
3. Open the normal Files app.
4. Tap the ZIP and choose Extract.
5. Open the Tumblr-Scraper folder.
6. Tap Tumblr Scraper - Android.py.
7. Choose Pydroid 3 if Android asks.
8. Press the Run button.

WINDOWS

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - Windows.bat.

MAC

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - macOS.command.

LINUX

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - Linux.desktop.

If you choose Run a new crawl, the program asks for the Tumblr blog name.
Enter only the name, for example:

vigilanceos

Your saved blogs are inside the Backups folder. The archive opens in your
normal browser when the backup finishes.

When the launcher opens, choose Open existing archive or Run a new crawl. The
optional viewer listens only on 127.0.0.1. Directly opening Backups/index.html
remains a supported fallback.





----------------------------------------------------------





MORE INFORMATION

If tapping the Python file does not offer Pydroid, open Pydroid 3, choose
Open, then open Downloads, Tumblr-Scraper, and Tumblr Scraper - Android.py.

Maximum posts:

- Blank means 300 newest posts.
- 1000 means inspect up to 1000 newest posts.
- 0 means no artificial maximum.

Image choice:

- Blank or no uses smaller Tumblr-provided images to save storage and data.
- Yes uses the largest Tumblr-provided image variant available.

The tool does not recompress or alter images.

Surrounding public context:

- This blog only saves the blog you entered.
- Nearby context also saves small samples from the public blogs this blog
  appears to interact with most.
- Explore blog neighborhood follows observed public interactions outward,
  within bounded depth and storage limits.

Context snapshots are samples, not complete backups of those blogs. The
target blog remains the priority, and context work resumes incrementally on
later runs. "Frequently interacting" does not necessarily mean the blogs
follow each other. The tool only knows what it can observe in captured public
posts. It does not know private interactions or follow-mutual status.

Network aggression:

- Gentle is slower and uses fewer simultaneous requests. It is least likely
  to trigger Tumblr's traffic limits.
- Normal is a balance between speed and request pressure.
- Urgent uses more simultaneous requests and removes most voluntary waiting.
  It may finish faster, but Tumblr is more likely to slow or temporarily
  block requests.

All supplied modes respect Tumblr's server-side throttling requests.

Run the launcher again later to update the backup. Existing posts are not
downloaded again unnecessarily. If the program closes or the connection
fails, run it again. Saved work remains on the device and unfinished saved
posts are processed first.

The archive is stored at:

Tumblr-Scraper/Backups/<blog>/


If Android's built-in file manager makes ordinary folder browsing difficult,
Fossify File Manager from Google Play is an optional free/open-source
alternative. It is not required.

This would not be possible without 
cebtenzzre's fork of tumblr-utils
https://cebtenzzre.github.io/tumblr-utils/

Or without Rael Dornfest's Blosxom
https://www.blosxom.com

Your blog always, always belonged to you,
not the owners of the servers who host it.


tumblr.vigilanceos.com
