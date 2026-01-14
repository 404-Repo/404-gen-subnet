from PIL import Image
import constants as const

def combine4(images: list[Image.Image]) -> Image.Image:
    """Combine 4 images into 2x2 grid"""
    row_width = const.IMG_WIDTH * 2 + const.GRID_VIEW_GAP
    column_height = const.IMG_HEIGHT * 2 + const.GRID_VIEW_GAP
    
    combined_image = Image.new("RGB", (row_width, column_height), color="black")
    
    # pil_images = [Image.fromarray(img.detach().cpu().numpy()) for img in images]
    
    combined_image.paste(images[0], (0, 0))
    combined_image.paste(images[1], (const.IMG_WIDTH + const.GRID_VIEW_GAP, 0))
    combined_image.paste(images[2], (0, const.IMG_HEIGHT + const.GRID_VIEW_GAP))
    combined_image.paste(images[3], (const.IMG_WIDTH + const.GRID_VIEW_GAP, const.IMG_HEIGHT + const.GRID_VIEW_GAP))
    
    return combined_image