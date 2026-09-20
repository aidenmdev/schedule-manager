"""The smooth scroller. Uses a hidden Tk root and a plain canvas. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_scroll -v"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schedule_gui as g


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.005)


class ScrollerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("300x1200+-20000+0")  # shown, so canvases have real sizes, but invisible and off screen
        self.canvas = tk.Canvas(self.root, width=200, height=200, yscrollincrement=1, scrollregion=(0, 0, 200, 2000))
        self.canvas.pack()
        self.root.update()
        pump(self.root, 0.2)
        self.origin = self.canvas.canvasy(0)  # a canvas starts a couple of pixels inside its border
        self.scroller = g.Scroller(self.root)

    def tearDown(self):
        self.scroller.cancel()
        self.root.destroy()

    def top(self):
        return round(self.canvas.canvasy(0) - self.origin)

    def test_moves_by_the_requested_distance_and_then_stops(self):
        self.scroller.add(self.canvas, 300)
        pump(self.root, 1.0)
        self.assertEqual(self.top(), 300)
        self.assertEqual(self.scroller.pending, {})
        self.assertFalse(self.scroller.running)

    def test_it_eases_instead_of_jumping(self):
        self.scroller.add(self.canvas, 400)
        pump(self.root, 0.04)
        partway = self.top()
        self.assertTrue(0 < partway < 400, partway)
        pump(self.root, 1.0)
        self.assertEqual(self.top(), 400)

    def test_many_small_events_add_up_and_share_one_animation(self):
        for _ in range(50):
            self.scroller.add(self.canvas, 6)
        self.assertEqual(len(self.scroller.pending), 1)
        pump(self.root, 1.2)
        self.assertEqual(self.top(), 300)

    def test_scrolling_back_cancels_out(self):
        self.scroller.add(self.canvas, 200)
        pump(self.root, 1.0)
        self.scroller.add(self.canvas, -80)
        pump(self.root, 1.0)
        self.assertEqual(self.top(), 120)

    def test_stops_at_the_bottom_and_the_top(self):
        self.scroller.add(self.canvas, 99999)
        pump(self.root, 1.5)
        self.assertAlmostEqual(self.canvas.yview()[1], 1.0, places=2)
        self.assertEqual(self.scroller.pending, {})
        self.scroller.add(self.canvas, -99999)
        pump(self.root, 1.5)
        self.assertAlmostEqual(self.canvas.yview()[0], 0.0, places=2)
        self.assertEqual(self.scroller.pending, {})

    def test_content_that_fits_does_not_move(self):
        small = tk.Canvas(self.root, width=200, height=200, yscrollincrement=1, scrollregion=(0, 0, 200, 100))
        small.pack()
        self.root.update()
        self.scroller.add(small, 300)
        self.assertEqual(self.scroller.pending, {})

    def test_two_areas_scroll_independently(self):
        other = tk.Canvas(self.root, width=200, height=200, yscrollincrement=1, scrollregion=(0, 0, 200, 2000))
        other.pack()
        self.root.update()
        self.scroller.add(self.canvas, 100)
        self.scroller.add(other, 250)
        pump(self.root, 1.2)
        self.assertEqual(self.top(), 100)
        self.assertEqual(round(other.canvasy(0) - self.origin), 250)

    def test_a_destroyed_area_is_forgotten(self):
        doomed = tk.Canvas(self.root, width=200, height=200, yscrollincrement=1, scrollregion=(0, 0, 200, 2000))
        doomed.pack()
        self.root.update()
        self.scroller.add(doomed, 500)
        doomed.destroy()
        self.scroller.add(self.canvas, 50)
        pump(self.root, 0.8)
        self.assertEqual(self.top(), 50)
        self.assertEqual(self.scroller.pending, {})

    def test_tiny_deltas_still_move(self):
        self.scroller.add(self.canvas, 0.9)
        pump(self.root, 0.3)
        self.assertEqual(self.top(), 1)

    def test_each_frame_does_little_work(self):
        """A long scroll is spread over many frames, so a heavy page never gets one huge redraw."""
        moves = []
        real = self.canvas.yview_scroll

        def spy(n, what):
            moves.append(abs(n))
            real(n, what)
        self.canvas.yview_scroll = spy
        self.scroller.add(self.canvas, 900)
        pump(self.root, 1.5)
        self.assertEqual(sum(moves), 900)
        self.assertLess(max(moves), 900 * 0.35)
        self.assertGreater(len(moves), 8)


if __name__ == "__main__":
    unittest.main()
