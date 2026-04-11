"""
Test script to verify thumbnail rendering produces lit/shaded output.
"""
import io
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set environment for headless rendering
os.environ['PYOPENGL_PLATFORM'] = 'osmesa'

try:
    from src.core.headless_thumbnail import render_thumbnail_from_glb_headless
    import trimesh
    import numpy as np

    # Create a simple cube mesh
    mesh = trimesh.creation.box(extents=[10, 10, 10])
    glb_bytes = mesh.export(file_type='glb')

    # Generate thumbnail
    print('Generating thumbnail...')
    thumbnail = render_thumbnail_from_glb_headless(glb_bytes, size=256, format='png')

    if thumbnail:
        with open('test_thumbnail_output.png', 'wb') as f:
            f.write(thumbnail)
        print(f'[OK] Thumbnail generated: {len(thumbnail)} bytes')
        print('[OK] Saved to: test_thumbnail_output.png')

        # Try to load and check pixel variance
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(thumbnail))
            img_array = np.array(img)
            variance = np.var(img_array)
            print(f'[OK] Image pixel variance: {variance:.2f}')

            if variance > 100:
                print('[PASS] Image has good lighting/shading (variance > 100)')
            else:
                print('[FAIL] Image appears flat (variance < 100)')
        except ImportError:
            print('[WARN] PIL not available - cannot verify pixel variance')
    else:
        print('[FAIL] Failed to generate thumbnail')
        sys.exit(1)

except Exception as e:
    print(f'[FAIL] Test failed: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
