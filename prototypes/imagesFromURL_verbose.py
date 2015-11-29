import re, os
import urllib2, httplib #For url manipulation and checking existence of urls
from bs4 import BeautifulSoup

imageCounter = 0

def updateImagePath(imagePath, oldRes, newRes):
    """Returns updated imagePath, based on existence of an image of better quality"""
    
    #Replace with new path name and check its existence:
    tempImagePath = imagePath.split(oldRes)[0] + newRes + imagePath.split(oldRes)[1]
    tempImagePathSplit = tempImagePath.split("/")            
    ##            #tempUrl = tempImagePath.split("/")[0] + "//" + tempImagePath.split("/")[2]
    tempUrl = tempImagePathSplit[2]            
    if len(tempImagePathSplit) == 5:
        tempPath = "/" + tempImagePathSplit[3] + "/" + tempImagePathSplit[4]
    elif len(tempImagePathSplit) == 4:
        tempPath = "/" + tempImagePathSplit[3]
    else:
        print "tempImagePathSplit length = " + str(len(tempImagePathSplit))
    ##            print tempUrl
    ##            print tempPath
    ##            tempUrl = "24.media.tumblr.com"
    ##            tempPath = "/1c9c5d13c9ce6a186bd35bb0bb3020ba/tumblr_mo6hcv59Nm1sqzffeo1_1280.jpg"
    if urlExists(tempUrl, tempPath): #If this is a valid url                
        imagePath = tempImagePath #Then set it to the current image url
    return imagePath


def urlExists(url, path):
    """Returns True or False depending on the existence of the given url and path"""    
    conn = httplib.HTTPConnection(url)
    conn.request('HEAD', path)
    response = conn.getresponse()
    conn.close()
    return response.status == 200 #True if the url exists, False if not

def removeValues(the_list, val):
    """Removes duplicate values in a list"""
    while val in the_list:
        the_list.remove(val)

        
def retrieveJpgs(url):
    """Retrieves images from a given url and writes them to a given folder"""
    global imageCounter #for editing this value
    
    page = BeautifulSoup(urllib2.urlopen(url))
    imagesFound = page.findAll('img')

    with open(title+'.txt', 'wb') as f:
        for part in page:
            for line in part:
                f.write(line.encode("UTF-8")) #Gotta convert to unicode from BeautifulSoup
        
    imageUrls=[]
    with open(title+'.txt') as f:
       for line in f:
          imageUrls.extend(re.findall(r'"([^"]*\.(?:jpg)[^"]*)"',line))

    imageList = []

    for item in imageUrls:    
        if len(item) > 4: #If we're dealing with a real URL
            if item.split('/')[0] != 'http:':            
                item = 'http:'+str(item)                    
            if item in imageList:
                pass
            else:
                imageList.append(item)        
        else: #If the entry isn't greater than 4 characters, it's probably the ".jpg" entry
            print "Removing all instances of the entry \' " + str(item) + "\'"
            removeValues(imageUrls, item) #and should be removed
    print imageList
    #Remove 500 url and replace with 1280 for higher resolution image path (easy hack):
    for imagePath in imageList:
        if "500" in imagePath: #If the image is only size 500 (the lower resolution image on most websites)
            print "Before: " + str(imagePath)
            imagePath = updateImagePath(imagePath, "500", "1280")
            print "After: " + str(imagePath)
        elif "250" in imagePath:
            print "Before: " + str(imagePath)
            tempPath = updateImagePath(imagePath, "250", "500")
            if imagePath == tempPath:
                imagePath = updateImagePath(imagePath, "250", "400")
            else:
                imagePath = tempPath
            print "After: " + str(imagePath)
        elif "400" in imagePath:
            print "Before: " + str(imagePath)
            imagePath = updateImagePath(imagePath, "400", "500")
            print "After: " + str(imagePath)
        else:
            pass
            #print imagePath
            #print "No change necessary."
##            print imagePath
##            print imagePath.split("500")            

##                print "New Image Path = " + str(imagePath)
        imageCounter += 1
        url = urllib2.urlopen(imagePath)
        img = url.read()
        pathname = 'images\\' + title + '\\'
        filename =  pathname + str(imageCounter) + '.jpg'    
        if os.path.exists(pathname):
            pass
        else:
            os.makedirs(pathname)
                        
        if os.path.exists(filename):
            pass
        else:
            with open(filename, 'wb') as f:
                f.write(img)

def retrievePngs(url):
    """Retrieves images from a given url and writes them to a given folder"""
    global imageCounter #for editing this value
    
    page = BeautifulSoup(urllib2.urlopen(url))
    imagesFound = page.findAll('img')

    with open(title+'.txt', 'wb') as f:
        for part in page:
            for line in part:
                f.write(line.encode("UTF-8")) #Gotta convert to unicode from BeautifulSoup
        
    imageUrls=[]
    with open(title+'.txt') as f:
       for line in f:
          imageUrls.extend(re.findall(r'"([^"]*\.(?:png)[^"]*)"',line))

    imageList = []

    for item in imageUrls:    
        if len(item) > 4: #If we're dealing with a real URL
            if item.split('/')[0] != 'http:':            
                item = 'http:'+str(item)                    
            if item in imageList:
                pass
            else:
                imageList.append(item)        
        else: #If the entry isn't greater than 4 characters, it's probably the ".jpg" entry
            print "Removing all instances of the entry \' " + str(item) + "\'"
            removeValues(imageUrls, item) #and should be removed
    print imageList
    #Remove 500 url and replace with 1280 for higher resolution image path (easy hack):
    for imagePath in imageList:
        if "500" in imagePath: #If the image is only size 500 (the lower resolution image on most websites)
            print "Before: " + str(imagePath)
            imagePath = updateImagePath(imagePath, "500", "1280")
            print "After: " + str(imagePath)
        elif "250" in imagePath:
            print "Before: " + str(imagePath)
            tempPath = updateImagePath(imagePath, "250", "500")
            if imagePath == tempPath:
                imagePath = updateImagePath(imagePath, "250", "400")
            else:
                imagePath = tempPath
            print "After: " + str(imagePath)
        elif "400" in imagePath:
            print "Before: " + str(imagePath)
            imagePath = updateImagePath(imagePath, "400", "500")
            print "After: " + str(imagePath)
        else:
            pass
            #print imagePath
            #print "No change necessary."
##            print imagePath
##            print imagePath.split("500")            

##                print "New Image Path = " + str(imagePath)
        imageCounter += 1
        url = urllib2.urlopen(imagePath)
        img = url.read()
        pathname = 'images\\' + title + '\\'
        filename =  pathname + str(imageCounter) + '.png'    
        if os.path.exists(pathname):
            pass
        else:
            os.makedirs(pathname)
                        
        if os.path.exists(filename):
            pass
        else:
            with open(filename, 'wb') as f:
                f.write(img)


def retrieveImages(url):
	retrieveJpgs(url=url)
	retrievePngs(url=url)


#TODO: Replace these with sys.args:
url_list = 'urls.txt' # File with list of urls
with open(url_list, 'r') as infile:
	for url in infile.readlines():
		originalUrl = url
		# numberOfPages = 1
		title = originalUrl.split('/')[-1].rstrip()
		print originalUrl
		print title
		retrieveImages(originalUrl)

# pageNum = 2        
# if numberOfPages > 1:
#     for i in range(0, numberOfPages):
#         updatedUrl = originalUrl + "page/" + str(pageNum)
#         print updatedUrl
#         retrieveImages(updatedUrl)        
#         pageNum += 1
        

