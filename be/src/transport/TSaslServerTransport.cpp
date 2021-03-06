// This file will be removed when the code is accepted into the Thrift library.
/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

#include "config.h"
#ifdef HAVE_SASL_SASL_H
#include <stdint.h>
#include <sstream>
#include <boost/shared_ptr.hpp>
#include <boost/scoped_ptr.hpp>

#include <thrift/transport/TBufferTransports.h>
#include "transport/TSaslTransport.h"
#include "transport/TSaslServerTransport.h"

using namespace std;
using namespace boost;
using namespace sasl;

namespace apache { namespace thrift { namespace transport {
TSaslServerTransport::TSaslServerTransport(shared_ptr<TTransport> transport)
   : TSaslTransport(transport) {
}

TSaslServerTransport::TSaslServerTransport(const string& mechanism,
                                           const string& protocol,
                                           const string& serverName, unsigned flags,
                                           const map<string, string>& props,
                                           const vector<struct sasl_callback>& callbacks, 
                                           shared_ptr<TTransport> transport) 
     : TSaslTransport(transport) {
  addServerDefinition(mechanism, protocol, serverName, flags, props, callbacks);
}

TSaslServerTransport:: TSaslServerTransport(
    const std::map<std::string, TSaslServerDefinition*>& serverMap,
    boost::shared_ptr<TTransport> transport) 
    : TSaslTransport(transport) {
  serverDefinitionMap_.insert(serverMap.begin(), serverMap.end());
}

/**
 * Set the server for this transport
 */
void TSaslServerTransport::setSaslServer(sasl::TSasl* saslServer) {
  if (isClient_) {
    throw TTransportException(
        TTransportException::INTERNAL_ERROR, "Setting server in client transport");
  }
  sasl_.reset(saslServer);
}

void TSaslServerTransport::handleSaslStartMessage() {
  uint32_t resLength;
  NegotiationStatus status;

  char* message = reinterpret_cast<char*>(receiveSaslMessage(&status, &resLength));
  if (status != TSASL_START) {
    stringstream ss;
    ss << "Expecting START status, received " << status;
    sendSaslMessage(TSASL_ERROR,
                    reinterpret_cast<const uint8_t*>(ss.str().c_str()), ss.str().size());
    throw TTransportException(ss.str());
  }
  map<string, TSaslServerDefinition*>::iterator defn =
      TSaslServerTransport::serverDefinitionMap_.find(message);
  if (defn == TSaslServerTransport::serverDefinitionMap_.end()) {
    stringstream ss;
    ss << "Unsupported mechanism type " << message;
    sendSaslMessage(TSASL_BAD,
                    reinterpret_cast<const uint8_t*>(ss.str().c_str()), ss.str().size());
    throw TTransportException(TTransportException::BAD_ARGS, ss.str());
  }
  // TODO: when should realm be used?
  string realm;
  TSaslServerDefinition* serverDefinition = defn->second;
  sasl_.reset(new TSaslServer(serverDefinition->protocol_,
                              serverDefinition->serverName_, realm,
                              serverDefinition->flags_,
                              &serverDefinition->callbacks_[0]));
  sasl_->evaluateChallengeOrResponse(reinterpret_cast<uint8_t*>(message),
                                     resLength, &resLength);

}

shared_ptr<TTransport> TSaslServerTransport::Factory::getTransport(
    boost::shared_ptr<TTransport> trans) {
  shared_ptr<TSaslServerTransport> retTransport;
  boost::lock_guard<boost::mutex> l (transportMap_mutex_);
  map<shared_ptr<TTransport>, shared_ptr<TSaslServerTransport> >::iterator transMap =
      transportMap_.find(trans);
  if (transMap == transportMap_.end()) {
    retTransport.reset(new TSaslServerTransport(serverDefinitionMap_, trans));
    retTransport.get()->open();
    transportMap_[trans] = retTransport;
  } else {
    retTransport = transMap->second;
  }
  return retTransport;
}

}}}
#endif
