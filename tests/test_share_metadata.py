from pathlib import Path
import unittest

from src.control_plane import PUBLIC_STATIC_FILES


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ShareMetadataTests(unittest.TestCase):
    def test_public_pages_publish_absolute_apple_link_preview_metadata(self) -> None:
        for relative_path in ("web/index.html", "web/not-found.html"):
            with self.subTest(relative_path=relative_path):
                html = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(
                    'property="og:image" content="https://tonytan.me/cybereditor/share-card.png"',
                    html,
                )
                self.assertIn('property="og:image:width" content="1200"', html)
                self.assertIn('property="og:image:height" content="630"', html)
                self.assertIn('name="twitter:card" content="summary_large_image"', html)

        share_card = (REPOSITORY_ROOT / "web/share-card.png").read_bytes()
        self.assertEqual(int.from_bytes(share_card[16:20], byteorder="big"), 1200)
        self.assertEqual(int.from_bytes(share_card[20:24], byteorder="big"), 630)

    def test_share_card_is_in_the_explicit_public_static_allowlist(self) -> None:
        self.assertEqual(PUBLIC_STATIC_FILES["/share-card.png"], "share-card.png")


if __name__ == "__main__":
    unittest.main()
