import os
import json
import requests

def write_json(filename=None, data=None):
    with open(filename, 'w') as outfile:
        json.dump(data, outfile)


def read_json(filename=None):
    with open(filename) as data_file:    
        data = json.load(data_file)
    return data


def read_html(filename=None):
    with open(filename, encoding='utf8') as data_file:
        data = data_file.read()
    return data


def perform_request(url=None, cookies=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows; U; Windows NT 5.1; it; rv:1.8.1.11) Gecko/20071127 Firefox/2.0.0.11'
    }
    
    print('Downloading {url}'.format(url=url))
    return requests.get(url=url, headers=headers, cookies=cookies)


def download_image(directory=None, url=None, cookies=None):
    imagetype_list = ['.jpg', '.JPG', '.png', '.PNG', '.gif', '.GIF']
    filename = url.split('/')[-1]
    
    if not any(filename.endswith(imagetype) for imagetype in imagetype_list):
        filename += '.jpg'

    filepath = os.path.join(directory, filename)
    
    response = perform_request(url=url, cookies=cookies)

    if response.status_code == 200:
        print('Successfully downloaded file, writing to {filepath}...\n'.format(filepath=filepath))
        with open(filepath, 'wb') as outfile:
            outfile.write(response.content)
