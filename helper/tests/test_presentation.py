import base64
import io

import numpy as np
from PIL import Image, ImageDraw


def page():
    im = Image.new('RGB', (400, 400), '#dddddd')
    draw = ImageDraw.Draw(im)
    draw.ellipse((90, 40, 310, 360), fill='white', outline='black', width=4)
    draw.rectangle((180, 100, 200, 290), fill='black')
    return im


def test_closed_bubble_cleanup_and_safe_frame_preserve_original():
    from balloonlens.presentation import prepare_presentation
    im = page()
    before = im.tobytes()
    result = prepare_presentation(im, [{'id': 0, 'bbox': [180, 100, 21, 191], 'src': 'text'}])[0]
    p = result['presentation']
    assert p['kind'] == 'bubble'
    x, y, w, h = p['frame']
    assert w > 80 and h > 150
    assert 90 < x < x+w < 310 and 40 < y < y+h < 360
    assert result['bbox'] == [180, 100, 21, 191]
    patch = Image.open(io.BytesIO(base64.b64decode(p['cleanup'].split(',')[1])))
    composed = im.convert('RGBA')
    composed.alpha_composite(patch, dest=tuple(p['cleanup_bbox'][:2]))
    assert composed.getpixel((190, 150))[:3] == (255, 255, 255)
    assert composed.getpixel((90, 200))[:3] == (0, 0, 0)
    assert im.tobytes() == before


def test_unenclosed_page_and_dark_background_do_not_get_erased():
    from balloonlens.presentation import prepare_presentation
    for color in ('white', 'black', '#555577'):
        im = Image.new('RGB', (400, 400), color)
        p = prepare_presentation(im, [{'id': 0, 'bbox': [180, 100, 21, 191], 'src': 'text'}])[0]['presentation']
        assert p['kind'] == 'overlay' and 'cleanup' not in p


def test_neighboring_bubbles_are_not_merged():
    from balloonlens.presentation import prepare_presentation
    im = Image.new('RGB', (500, 300), '#ddd')
    d = ImageDraw.Draw(im)
    for x in (20, 270):
        d.ellipse((x, 20, x+210, 280), fill='white', outline='black', width=4)
        d.rectangle((x+90, 100, x+105, 180), fill='black')
    out = prepare_presentation(im, [{'id': i, 'bbox': [x+90, 100, 16, 81], 'src': 'a'} for i,x in enumerate((20,270))])
    assert all(s['presentation']['kind'] == 'bubble' for s in out)
    assert out[0]['presentation']['frame'][0] + out[0]['presentation']['frame'][2] < out[1]['presentation']['frame'][0]


def test_two_separate_texts_in_one_bubble_do_not_share_full_frame():
    from balloonlens.presentation import prepare_presentation
    im = page()
    result = prepare_presentation(im, [
        {'id': 0, 'bbox': [180, 100, 21, 70], 'src': 'a'},
        {'id': 1, 'bbox': [180, 200, 21, 70], 'src': 'b'},
    ])
    first, second = [s['presentation']['frame'] for s in result]
    assert first[1]+first[3] <= second[1] or second[1]+second[3] <= first[1]


def test_narrow_vertical_bubble_frame_favors_horizontal_korean():
    from balloonlens.presentation import prepare_presentation
    im=Image.new('RGB',(400,500),'#ddd')
    d=ImageDraw.Draw(im)
    d.ellipse((130,40,270,460),fill='white',outline='black',width=4)
    d.rectangle((185,130,200,350),fill='black')
    p=prepare_presentation(im,[{'id':0,'bbox':[185,130,16,221],'src':'a'}])[0]['presentation']
    assert p['kind']=='bubble'
    assert p['frame'][2]/p['frame'][3]>=0.6


def test_shared_bubble_preserves_ocr_reading_order():
    from balloonlens.presentation import prepare_presentation
    im=page()
    result=prepare_presentation(im,[{'id':0,'bbox':[195,120,15,130],'src':'first'},
                                    {'id':1,'bbox':[170,100,15,80],'src':'second'}])
    assert result[0]['presentation']['frame'][1] < result[1]['presentation']['frame'][1]


def test_cleanup_does_not_erase_neighboring_segment():
    from balloonlens.presentation import prepare_presentation
    im=page()
    draw=ImageDraw.Draw(im)
    draw.rectangle((185,172,205,200),fill='black')
    result=prepare_presentation(im,[{'id':0,'bbox':[180,100,21,70],'src':'a'},
                                    {'id':1,'bbox':[185,172,21,29],'src':'b'}])
    p=result[0]['presentation']
    patch=Image.open(io.BytesIO(base64.b64decode(p['cleanup'].split(',')[1])))
    px,py,_,_=p['cleanup_bbox']
    if py+patch.height>172:
        assert patch.getpixel((190-px,172-py))[3]==0


def test_faint_antialiasing_is_removed_with_the_glyph():
    from balloonlens.presentation import prepare_presentation
    im=page()
    ImageDraw.Draw(im).rectangle((176,100,176,290),fill=(250,250,250))
    p=prepare_presentation(im,[{'id':0,'bbox':[176,100,25,191],'src':'a'}])[0]['presentation']
    patch=Image.open(io.BytesIO(base64.b64decode(p['cleanup'].split(',')[1])))
    x,y,_,_=p['cleanup_bbox']
    assert patch.getpixel((176-x,150-y))[3]==255
