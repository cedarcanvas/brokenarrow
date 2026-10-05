# USPS labeling lists (optional)

Put USPS labeling-list files here to add the official sorting chain
(ZIP prefix -> plant -> regional center -> package hub) to the map.

Where to get them (free): https://fast.usps.com/fast/fastApp/resources/labelListFiles.action
Pick the newest effective date and download:

- **L002** 3-Digit ZIP Code Prefix Matrix (most useful: every level in one file)
- **L005** 3-digit ZIP Code prefix groups, SCF sortation (backup)
- **L004** 3-digit ZIP Code prefix groups, ADC sortation (backup)
- the NDC (Network Distribution Center) list, if offered

Any format is fine (text, Excel or zip). Keep them in this folder, not in
`raw/usps/`: files in `raw/usps/` are read as delivery-day files.

To add files on github.com: open this folder, then **Add file -> Upload files**.
