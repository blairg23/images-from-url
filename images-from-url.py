import re, os
from bs4 import BeautifulSoup
from utilities import write_json, read_json, read_html, perform_request, download_image


def get_image_urls_from_html(html=None, src_tag=None):
    soup = BeautifulSoup(html, 'html.parser')
    image_urls = []
    for image in soup.findAll('img'):
        image_url = image.get(src_tag)
        if image_url is not None:
            image_urls.append(image_url)
        else:
            print(image)
    return image_urls
        #print(image.get(src_tag))

if __name__ == '__main__':
    url = 'http://na.finalfantasyxiv.com/'
    url_name = 'ffxiv'
    output_directory = os.path.join('images', 'websites', url_name)

    # If the output folder doesn't exist, create it:
    try:
        os.stat(output_directory)
    except:
        os.makedirs(output_directory)

    response = perform_request(url=url)

    html = response.content.decode('utf-8')

    src_tag = 'src'

    image_urls = get_image_urls_from_html(html=html, src_tag=src_tag)

    for image_url in image_urls:
        download_image(directory=output_directory, url=image_url)

    #regex = '(http(s?):)([/|.|\w|\s|-])*\.(?:jpg|gif|png)'

    #m = re.search(regex, html)

