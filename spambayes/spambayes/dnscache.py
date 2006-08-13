# Copyright 2004, Matthew Dixon Cowles <matt@mondoinfo.com>.
# Distributable under the same terms as the Python programming language.
# Inspired by the KevinL's cache included with PyDNS.
# Provided with NO WARRANTY.

# Version 0.1 2004 06 27
# Version 0.11 2004 07 06 Fixed zero division error in __del__

import DNS # From http://sourceforge.net/projects/pydns/

import sys
import os
import operator
import time
import types
import socket
try:
    import cPickle as pickle
except ImportError:
    import pickle

from spambayes.Options import options

kCheckForPruneEvery=20
kMaxTTL=60 * 60 * 24 * 7 # One week
kPruneThreshold=1500 # May go over slightly; numbers chosen at random
kPruneDownTo=1000


class lookupResult(object):
    #__slots__=("qType","answer","question","expiresAt","lastUsed")

    def __init__(self,qType,answer,question,expiresAt,now):
        self.qType=qType
        self.answer=answer
        self.question=question
        self.expiresAt=expiresAt
        self.lastUsed=now
        return None


# From ActiveState's Python cookbook
# Yakov Markovitch, Fast sort the list of objects by object's attribute
# http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/52230
def sort_by_attr(seq, attr):
    """Sort the sequence of objects by object's attribute

    Arguments:
    seq  - the list or any sequence (including immutable one) of objects to sort.
    attr - the name of attribute to sort by

    Returns:
    the sorted list of objects.
    """
    #import operator

    # Use the "Schwartzian transform"
    # Create the auxiliary list of tuples where every i-th tuple has form
    # (seq[i].attr, i, seq[i]) and sort it. The second item of tuple is needed not
    # only to provide stable sorting, but mainly to eliminate comparison of objects
    # (which can be expensive or prohibited) in case of equal attribute values.
    intermed = map(None, map(getattr, seq, (attr,)*len(seq)), xrange(len(seq)), seq)
    intermed.sort()
    return map(operator.getitem, intermed, (-1,) * len(intermed))


class cache:
    def __init__(self,dnsServer=None,cachefile=""):
    # These attributes intended for user setting
        self.printStatsAtEnd=False

        # As far as I can tell from the standards,
        # it's legal to have more than one PTR record
        # for an address. That is, it's legal to get
        # more than one name back when you do a
        # reverse lookup on an IP address. I don't
        # know of a use for that and I've never seen
        # it done. And I don't think that most
        # people would expect it. So forward ("A")
        # lookups always return a list. Reverse
        # ("PTR") lookups return a single name unless
        # this attribute is set to False.
        self.returnSinglePTR=True

        # How long to cache an error as no data
        self.cacheErrorSecs=5*60

        # How long to wait for the server
        self.dnsTimeout=10

        # Some servers always return a TTL of zero.
        # In those cases, turning this up a bit is
        # probably reasonable.
        self.minTTL=0

        # end of user-settable attributes

        self.cachefile = os.path.expanduser(cachefile)
        if self.cachefile and os.path.exists(self.cachefile):
            self.caches = pickle.load(open(self.cachefile, "rb"))
        else:
            self.caches = {"A": {}, "PTR": {}}

        if options["globals", "verbose"]:
            if self.caches["A"] or self.caches["PTR"]:
                print >> sys.stderr, "opened existing cache with",
                print >> sys.stderr, len(self.caches["A"]), "A records",
                print >> sys.stderr, "and", len(self.caches["PTR"]),
                print >> sys.stderr, "PTR records"
            else:
                print >> sys.stderr, "opened new cache"

        self.hits=0 # These two for statistics
        self.misses=0
        self.pruneTicker=0

        if dnsServer==None:
            DNS.DiscoverNameServers()
            self.queryObj=DNS.DnsRequest()
        else:
            self.queryObj=DNS.DnsRequest(server=dnsServer)
        return None

    def close(self):
        if self.printStatsAtEnd:
            self.printStats()
        if self.cachefile:
            pickle.dump(self.caches, open(self.cachefile, "wb"))

    def printStats(self):
        for key,val in self.caches.items():
            totAnswers=0
            for item in val.values():
                totAnswers+=len(item)
            print >> sys.stderr, "cache", key, "has", len(self.caches[key]),
            print >> sys.stderr, "question(s) and", totAnswers, "answer(s)"
        if self.hits+self.misses==0:
            print >> sys.stderr, "No queries"
        else:
            print >> sys.stderr, self.hits, "hits,", self.misses, "misses",
            print >> sys.stderr, "(%.1f%% hits)" % \
                  (self.hits/float(self.hits+self.misses)*100)

    def prune(self,now):
        # I want this to be as fast as reasonably possible.
        # If I didn't, I'd probably do various things differently
        # Is there a faster way to do this?
        allAnswers=[]
        for cache in self.caches.values():
            for val in cache.values():
                allAnswers += val

        allAnswers=sort_by_attr(allAnswers,"expiresAt")
        allAnswers.reverse()

        while True:
            if allAnswers[-1].expiresAt>now:
                break
            answer=allAnswers.pop()
            c=self.caches[answer.qType]
            c[answer.question].remove(answer)
            if len(c[answer.question])==0:
                del c[answer.question]

        if options["globals", "verbose"]:
            self.printStats()

        if len(allAnswers)<=kPruneDownTo:
            return None

        # Expiring didn't get us down to the size we want, so delete
        # some entries least-recently-used-wise. I'm not by any means
        # sure that this is the best strategy, but as yet I don't have
        # data to test different strategies.
        allAnswers=sort_by_attr(allAnswers,"lastUsed")
        allAnswers.reverse()
        numToDelete=len(allAnswers)-kPruneDownTo
        for count in range(numToDelete):
            answer=allAnswers.pop()
            c=self.caches[answer.qType]
            c[answer.question].remove(answer)
            if len(c[answer.question])==0:
                del c[answer.question]

        return None


    def formatForReturn(self,listOfObjs):
        if len(listOfObjs)==1 and listOfObjs[0].answer==None:
            return []

        if listOfObjs[0].qType=="PTR" and self.returnSinglePTR:
            return listOfObjs[0].answer

        return [ obj.answer for obj in listOfObjs ]


    def lookup(self,question,qType="A"):
        qType=qType.upper()
        if qType not in ("A","PTR"):
            raise ValueError,"Query type must be one of A, PTR"

        now=int(time.time())

        # Finding the len() of a dictionary isn't an expensive operation
        # but doing it twice for every lookup isn't necessary.
        self.pruneTicker+=1
        if self.pruneTicker==kCheckForPruneEvery:
            self.pruneTicker=0
            if len(self.caches["A"])+len(self.caches["PTR"])>kPruneThreshold:
                self.prune(now)

        cacheToLookIn=self.caches[qType]

        try:
            answers=cacheToLookIn[question]
        except KeyError:
            pass
        else:
            assert len(answers)>0
            ind=0
            # No guarantee that expire has already been done
            while ind<len(answers):
                thisAnswer=answers[ind]
                if thisAnswer.expiresAt<now:
                    del answers[ind]
                else:
                    thisAnswer.lastUsed=now
                    ind+=1

            if len(answers)==0:
                del cacheToLookIn[question]
            else:
                self.hits+=1
                return self.formatForReturn(answers)

        # Not in cache or we just expired it
        self.misses+=1

        if qType=="PTR":
            qList=question.split(".")
            qList.reverse()
            queryQuestion=".".join(qList)+".in-addr.arpa"
        else:
            queryQuestion=question

        # where do we get NXDOMAIN?
        try:
            reply=self.queryObj.req(queryQuestion,qtype=qType,timeout=self.dnsTimeout)
        except DNS.Base.DNSError,detail:
            if detail.args[0]<>"Timeout":
                print "Error, fixme",detail
                print "Question was",queryQuestion
                print "Origianal question was",question
                print "Type was",qType
            objs=[ lookupResult(qType,None,question,self.cacheErrorSecs+now,now) ]
            cacheToLookIn[question]=objs # Add to format for return?
            return self.formatForReturn(objs)
        except socket.gaierror,detail:
            print "DNS connection failure:", self.queryObj.ns, detail
            print "Defaults:", DNS.defaults

        objs=[]
        for answer in reply.answers:
            if answer["typename"]==qType:
                # PyDNS returns TTLs as longs but RFC 1035 says that the
                # TTL value is a signed 32-bit value and must be positive,
                # so it should be safe to coerce it to a Python integer.
                # And anyone who sets a time to live of more than 2^31-1
                # seconds (68 years and change) is drunk.
                # Arguably, I ought to impose a maximum rather than continuing
                # with longs (int(long) returns long in recent versions of Python).
                ttl=max(min(int(answer["ttl"]),kMaxTTL),self.minTTL)
                # RFC 2308 says that you should cache an NXDOMAIN for the
                # minimum of the minimum field of the SOA record and the TTL
                # of the SOA.
                if ttl>0:
                    item=lookupResult(qType,answer["data"],question,ttl+now,now)
                    objs.append(item)

        if len(objs)>0:
            cacheToLookIn[question]=objs
            return self.formatForReturn(objs)

        # Probably SERVFAIL or the like
        if len(reply.authority)==0:
            objs=[ lookupResult(qType,None,question,self.cacheErrorSecs+now,now) ]
            cacheToLookIn[question]=objs
            return self.formatForReturn(objs)


        # No such host
        #
        # I don't know in what circumstances you'd have more than one authority,
        # so I'll just assume that the first is what we want.
        #
        # RFC 2308 specifies that this how to decide how long to cache an
        # NXDOMAIN.
        auth=reply.authority[0]
        auTTL=int(auth["ttl"])
        for item in auth["data"]:
            if type(item)==types.TupleType and item[0]=="minimum":
                auMin=int(item[1])
                cacheNeg=min(auMin,auTTL)
                break
        else:
            cacheNeg=auTTL
        objs=[ lookupResult(qType,None,question,cacheNeg+now,now) ]

        cacheToLookIn[question]=objs
        return self.formatForReturn(objs)


def main():
    import transaction
    c=cache(cachefile=os.path.expanduser("~/.dnscache"))
    c.printStatsAtEnd=True
    for host in ["www.python.org", "www.timsbloggers.com",
                 "www.seeputofor.com", "www.completegarbage.tv",
                 "www.tradelinkllc.com"]:
        print "checking", host
        now=time.time()
        ips=c.lookup(host)
        print ips,time.time()-now
        now=time.time()
        ips=c.lookup(host)
        print ips,time.time()-now

        if ips:
            ip=ips[0]
            now=time.time()
            name=c.lookup(ip,qType="PTR")
            print name,time.time()-now
            now=time.time()
            name=c.lookup(ip,qType="PTR")
            print name,time.time()-now
        else:
            print "unknown"

    c.close()

    return None

if __name__=="__main__":
    main()
