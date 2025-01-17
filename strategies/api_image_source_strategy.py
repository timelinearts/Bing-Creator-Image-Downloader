import asyncio
import json
import logging
import os
import re
from asyncio import Semaphore
from datetime import timezone, date
from typing import List

from dateutil import parser as dateutil_parser

from models.image import Image
from strategies.image_source_strategy import ImageSourceStrategy
from utilities.config import Config
from utilities.image_utility import ImageUtility
from utilities.image_validator import ImageValidator
from utilities.network_utility import NetworkUtility


class APIImageSourceStrategy(ImageSourceStrategy):
    """
    Strategy class for getting images from the collection API.
    """

    async def get_images(self) -> List[Image]:
        """
        Gathers all images in the collections.
        :return: A list containing :class:`BingCreatorImage` objects.
        :rtype: List[Image]
        """
        cookie = os.getenv('COOKIE')
        if cookie:
            logging.debug(f"Loaded cookie ending with: {cookie[-16:]}")
        else:
            raise Exception("No cookie was found in the .env file.")
        images = APIImageSourceStrategy.get_image_data()
        await APIImageSourceStrategy.__gather_additional_data(images)

        return images

    @staticmethod
    def get_image_data():
        """
        Gathers all necessary data for each image from all collections.
        :return: A list containing :class:`BingCreatorImage` objects.
        :rtype: List[Image]
        """
        logging.info(f"Fetching metadata of collections...")
        header = {
            "Content-Type": "application/json",
            "cookie": os.getenv('COOKIE'),
            "sid": "0"
        }
        body = {
            "collectionItemType": "all",
            "maxItemsToFetch": 1000,
            "shouldFetchMetadata": True
        }
        response = NetworkUtility.create_session().post(
            url='https://www.bing.com/mysaves/collections/get?sid=0',
            headers=header,
            data=json.dumps(body)
        )
        if response.status_code == 200:
            collection_dict = response.json()
            if len(collection_dict['collections']) == 0:
                raise Exception('No collections were found for the given cookie.')
            gathered_image_data = []
            index = 1
            for collection in collection_dict['collections']:
                if ImageValidator.should_add_collection_to_images(collection):
                    if Config().value['debug']['debug']:
                        with open('collection_dict_dump_debug.json', 'w') as f:
                            f.write(json.dumps(collection))
                    for item in collection['collectionPage']['items']:
                        if ImageValidator.should_add_item_to_images(item):
                            custom_data = json.loads(item['content']['customData'])
                            image_page_url = custom_data['PageUrl']
                            image_url = custom_data['MediaUrl']
                            image_prompt = custom_data['ToolTip']
                            date_modified = item['dateModified']
                            collection_name = collection['title']
                            image_urls = [(1, image_url)]
                            if 'thumbnails' in item['content']:
                                thumbnail_raw = item['content']['thumbnails'][0]['thumbnailUrl']
                                thumbnail_url = re.match('^[^&]+', thumbnail_raw).group(0)
                                image_urls.append((3, thumbnail_url))
                            pattern = r'Image \d of \d$'
                            image_prompt = re.sub(pattern, '', image_prompt)
                            image = Image(
                                image_urls=image_urls,
                                prompt=image_prompt,
                                collection_name=collection_name,
                                page_url=image_page_url,
                                index=str(index).zfill(4),
                                date_modified=date_modified
                            )
                            gathered_image_data.append(image)
                            index += 1
            return gathered_image_data
        else:
            raise Exception(f"Fetching collection failed with Error code "
                            f"{response.status_code}: {response.reason};{response.text}")

    @staticmethod
    async def __gather_additional_data(images) -> None:
        """
        Sets the creation date and adds additional fetch URLs for each image.
        :return: None
        """
        semaphore = Semaphore(250)
        tasks = [
            APIImageSourceStrategy.__set_additional_data(image, semaphore)
            for image
            in images
        ]
        await asyncio.gather(*tasks)

    @staticmethod
    async def __set_additional_data(image: Image, semaphore: asyncio.Semaphore) -> None:
        """
        Fetches and sets additional data from the detail API.
        :param semaphore: Limits concurrency for the request.
        :param image: :class:`BingCreatorImage` object to set the `creation_date` value for.
        :return: None
        """
        extracted_ids = await ImageUtility.extract_set_and_image_id(image.page_url)
        image_set_id = extracted_ids['image_set_id']
        image_id = extracted_ids['image_id']
        response_image = await ImageUtility.get_detail_image(image_set_id, image_id, semaphore)
        if response_image is not None:
            creation_date_string = response_image['datePublished']
            if not any(response_image['contentUrl'] == url for _, url in image.image_urls):
                image.image_urls.append((2, response_image['contentUrl']))
            if not any(response_image['thumbnailUrl'] == url for _, url in image.image_urls):
                image.image_urls.append((4, response_image['thumbnailUrl']))
            image.image_urls = sorted(image.image_urls, key=lambda url: url[0])
        elif image.date_modified is not None:
            creation_date_string = image.date_modified
        else:
            creation_date_string = date.today().isoformat()

        creation_date_object = dateutil_parser.parse(creation_date_string).astimezone(timezone.utc)
        creation_date_string_formatted = creation_date_object.strftime('%Y-%m-%dT%H%MZ')
        image.creation_date = creation_date_string_formatted
