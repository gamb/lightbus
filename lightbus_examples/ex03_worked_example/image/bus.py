"""A simple image resizing service"""
from lightbus import Api, Event

class ImageApi(Api):

    class Meta:
        name = 'image'

    def resize(self, url, width, height):
        """Resize image at the given URL and return new URL"""
        # This is a demo, so just return an animal picture of the correct size
        return f'https://placeimg.com/{width}/{height}/animals?_={url}'
