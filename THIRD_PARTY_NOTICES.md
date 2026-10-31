# Third-party software

The setup script downloads **PixCut-App** by popcornhax:
https://github.com/popcornhax/PixCut-App, MIT License.
Its source and original license remain in `vendor/PixCut-App/`; they are not
copied into this repository. The exact revision is recorded in `UPSTREAM.md`.
This project's modules import its USB transport, protocol client, raster
padding helper, and PLT serializer.

Python packages are installed into `.venv/` from `requirements.lock`, not
redistributed in the Git repository. These include Pillow, pillow-heif, NumPy,
SciPy, scikit-image, Shapely, PyUSB, libusb-package, svgelements, svgpathtools,
and their dependencies. Their respective licenses remain in the installed
package metadata. Homebrew provides libusb separately.

Liene and PixCut are names of their respective owners. This is an independent
project and is not affiliated with or endorsed by the manufacturer.
