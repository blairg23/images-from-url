import re, glob, os

def removeValues(the_list, val):
    while val in the_list:
        the_list.remove(val)

url = "http://imgur.com/a/1O9Ym"
import urllib2
from bs4 import BeautifulSoup
page = BeautifulSoup(urllib2.urlopen(url))
imagesFound = page.findAll('img')

with open('test.txt') as f:
    data = f.read()

images=[]
with open('test.txt') as f:
   for line in f:
      images.extend(re.findall(r'"([^"]*\.(?:jpg)[^"]*)"',line))

imageList = []

for item in images:    
    if len(item) > 4:        
        if item.split('/')[0] != 'http:':            
            item = 'http:'+str(item)                    
        if item in imageList:
            pass
        else:
            imageList.append(item)        
    else:
        print item
        removeValues(images, item)

counter = 0
for item in imageList:
    counter += 1
    url = urllib2.urlopen(item)
    img = url.read()
    filename = 'images\\' + str(counter) + '.jpg'    
    if os.path.exists(filename):
        pass
    else:
        with open(filename, 'wb') as f:
            f.write(img)


