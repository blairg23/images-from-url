import requests
import os

from bs4 import BeautifulSoup

def dump_image_files(url_list=None, folder_name=None):
    for url in url_list:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            image_name = url.split('/')[-1].rstrip()
            full_path = os.path.join('images', folder_name, image_name)
            if not os.path.exists(os.path.dirname(full_path)): # If the folder doesn't exist yet,
                os.makedirs(os.path.dirname(full_path)) # Create it
            with open(full_path, 'wb') as out_file: # Then write to it              
                out_file.write(response.content)

def retrieve_image_urls(url=None, debug=False):
    images = []
    html_file = requests.get(url)
    soup = BeautifulSoup(html_file.content, 'html.parser')
    
    if debug:
        print soup.prettify()

    for link in soup.find_all('a'):
        if '.jpg' in link.get('href') or '.png' in link.get('href'):
            images.append(link.get('href'))

    image_urls = []
    for image in images:
        if image[0] == '/': # If the image url is a relative link
            image_url = url+image # Add the original url as a prefix
        else:
            image_url = image
        image_urls.append(str(image_url))
    return image_urls

def get_gallery_images(gallery_urls=None):
    url_list = gallery_urls # File with list of urls
    for url in url_list:
        original_url = url.rstrip() # Strip that nasty \n
        title = original_url.split('/')[-1].rstrip() # Strip that nasty \n
        print original_url
        print title
    #   image_urls = retrieve_image_urls(url=original_url)
    #   dump_image_files(url_list=image_urls, folder_name=title)
    # with open(url_list, 'r') as infile:
    #   for url in infile.readlines():
    #       original_url = url.rstrip() # Strip that nasty \n
    #       # numberOfPages = 1
    #       title = original_url.split('/')[-1].rstrip() # Strip that nasty \n
    #       print original_url
    #       print title
    #       image_urls = retrieve_image_urls(url=original_url)
    #       dump_image_files(url_list=image_urls, folder_name=title)


def retrieve_gallery_urls(url=None, class_name=None, debug=False):
    gallery_urls = []
    html_file = requests.get(url)
    soup = BeautifulSoup(html_file.content, 'html.parser')

    if debug:
        print soup.prettify()

    for link in soup.find_all('a'):
        if link.get('class') is not None and class_name in link.get('class'):
            if 'galleries' in link.get('href'):
                gallery_urls.append(link.get('href'))
    return gallery_urls

url_list = 'urls.txt'

with open(url_list, 'r') as infile:
    for url in infile.readlines():
        original_url = url.rstrip() # Strip that nasty \n       
        galleries = retrieve_gallery_urls(url=original_url, class_name='track') # This class name signifies a gallery (in this case, 'track')