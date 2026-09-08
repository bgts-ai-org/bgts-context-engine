# Assets

`social-preview.svg` is the source; `social-preview.png` is the 1280x640 raster GitHub wants
for the repository social preview (Settings, General, Social preview) and the banner both
READMEs load.

Edit the SVG, then regenerate the PNG:

```bash
npx --yes sharp-cli --input docs/assets/social-preview.svg --output /tmp resize 1280 640
mv /tmp/social-preview.svg docs/assets/social-preview.png
```

`sharp-cli` keeps the input filename in the output directory, so the raster lands with an
`.svg` name and has to be renamed. Write it to a scratch directory rather than in place,
which otherwise overwrites the source with PNG bytes.
