from balloonlens.ocr import dedupe_rects, group_boxes, order_rects, tile_ranges


def test_tile_overlap_duplicates_are_merged():
    full = [200, 1073, 580, 1154]
    cut_at_tile_edge = [210, 1073, 560, 1110]
    other_line = [200, 1200, 580, 1240]
    assert dedupe_rects([cut_at_tile_edge, full, other_line]) == [full, other_line]


def test_short_image_is_single_tile():
    assert tile_ranges(800, 1200) == [(0, 1200)]


def test_tall_image_tiles_overlap_and_cover():
    ranges = tile_ranges(800, 6000)
    assert ranges[0][0] == 0 and ranges[-1][1] == 6000
    for (_, prev_end), (start, _) in zip(ranges, ranges[1:]):
        assert start < prev_end  # overlap so boundary balloons aren't lost


def test_vertical_columns_in_one_balloon_merge():
    # three Japanese columns, 30px wide, 10px apart
    cols = [[100, 50, 130, 250], [140, 50, 170, 230], [180, 50, 210, 260]]
    assert len(group_boxes(cols)) == 1


def test_separate_balloons_stay_apart():
    a = [[100, 50, 130, 250], [140, 50, 170, 230]]
    b = [[400, 60, 430, 240]]
    groups = group_boxes(a + b)
    assert sorted(len(g) for g in groups) == [1, 2]


def test_rtl_order_reads_right_to_left_then_down():
    right_top = [500, 10, 600, 200]
    left_top = [100, 20, 200, 180]
    bottom = [300, 400, 400, 600]
    rects = [left_top, bottom, right_top]
    assert order_rects(rects, "rtl") == [2, 0, 1]
    assert order_rects(rects, "ltr") == [0, 2, 1]
