import requests
import json

url_list = 'urls.txt' # File with list of urls
with open(url_list, 'r') as infile:
    for url in infile.readlines():
        originalUrl = url
        # numberOfPages = 1
        title = originalUrl.split('/')[-1].rstrip()
        print originalUrl
        print title
        retrieveImages(originalUrl)