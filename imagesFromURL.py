import requests
from bs4 import BeautifulSoup


def retrieveImages(url=None, debug=False):
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
        image_urls.append(image_url)
    return image_urls

url_list = 'urls.txt' # File with list of urls
with open(url_list, 'r') as infile:
    for url in infile.readlines():
        originalUrl = url
        # numberOfPages = 1
        title = originalUrl.split('/')[-1].rstrip()
        print originalUrl
        print title
        retrieveImages(originalUrl)