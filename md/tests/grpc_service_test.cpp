// The §4.3 gRPC surface: a real server on loopback, a real client stub, and a
// scenario driven through the full pipeline behind it.

#include <memory>
#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>
#include <grpcpp/grpcpp.h>

#include "md/mock_source.hpp"
#include "md/pipeline.hpp"
#include "md/service.hpp"
#include "md_service.grpc.pb.h"
#include "test_support.hpp"

using tevnnis::md::MarketDataServiceImpl;
using tevnnis::md::MdPipeline;
using tevnnis::md::MockMarketDataSource;
using tevnnis::md::ReplayClock;
using tevnnis::md::SourceEvent;
using tevnnis::md::SourceEventTs;
using tevnnis::md::test::BaseConfig;
using tevnnis::md::test::ScenarioFile;

namespace {

// A pipeline fed by the deterministic scenario, fronted by a real gRPC server.
class ServedPipeline {
   public:
    ServedPipeline() : pipeline_(BaseConfig(), clock_.AsClock()), service_(pipeline_) {
        MockMarketDataSource source(ScenarioFile("stage3_pipeline.json"));
        source.run([&](const SourceEvent& event) {
            clock_.set_now_ms(SourceEventTs(event));
            pipeline_.OnSourceEvent(event);
        });

        int port = 0;
        grpc::ServerBuilder builder;
        builder.AddListeningPort("127.0.0.1:0", grpc::InsecureServerCredentials(), &port);
        builder.RegisterService(&service_);
        server_ = builder.BuildAndStart();
        REQUIRE(server_ != nullptr);
        REQUIRE(port != 0);

        stub_ = tevnnis::MarketData::NewStub(grpc::CreateChannel(
            "127.0.0.1:" + std::to_string(port), grpc::InsecureChannelCredentials()));
    }

    ~ServedPipeline() { server_->Shutdown(); }

    ServedPipeline(const ServedPipeline&) = delete;
    ServedPipeline& operator=(const ServedPipeline&) = delete;

    tevnnis::MarketData::Stub& stub() { return *stub_; }

   private:
    ReplayClock clock_;
    MdPipeline pipeline_;
    MarketDataServiceImpl service_;
    std::unique_ptr<grpc::Server> server_;
    std::unique_ptr<tevnnis::MarketData::Stub> stub_;
};

std::vector<std::string> EventIds(const tevnnis::PullResponse& response) {
    std::vector<std::string> ids;
    for (const auto& event : response.events()) {
        ids.push_back(event.event_id());
    }
    return ids;
}

}  // namespace

TEST_CASE("PullDecisionBatch serves a prioritized batch over gRPC", "[md][grpc]") {
    ServedPipeline served;

    tevnnis::PullRequest request;
    request.set_max_events(10);
    request.set_min_priority(tevnnis::LOW);

    grpc::ClientContext context;
    tevnnis::PullResponse response;
    const grpc::Status status =
        served.stub().PullDecisionBatch(&context, request, &response);

    REQUIRE(status.ok());
    REQUIRE(response.events_size() == 7);
    REQUIRE(response.events(0).priority() == tevnnis::CRITICAL);
    REQUIRE(response.events(6).priority() == tevnnis::LOW);
    REQUIRE(response.snapshots_size() == 2);
    REQUIRE(response.dropped_count() == 4);
    REQUIRE_FALSE(response.next_cursor().empty());
}

TEST_CASE("A second pull with next_cursor delivers nothing new", "[md][grpc]") {
    ServedPipeline served;

    tevnnis::PullRequest request;
    request.set_max_events(10);

    tevnnis::PullResponse first;
    {
        grpc::ClientContext context;
        REQUIRE(served.stub().PullDecisionBatch(&context, request, &first).ok());
    }
    REQUIRE(first.events_size() == 7);

    request.set_since_cursor(first.next_cursor());
    tevnnis::PullResponse second;
    {
        grpc::ClientContext context;
        REQUIRE(served.stub().PullDecisionBatch(&context, request, &second).ok());
    }

    REQUIRE(second.events_size() == 0);
    REQUIRE(second.dropped_count() == 0);
    REQUIRE(second.snapshots_size() == 2);  // context is sent every round
    REQUIRE(second.next_cursor() == first.next_cursor());
}

TEST_CASE("Sector and priority filters travel over the wire", "[md][grpc]") {
    ServedPipeline served;

    tevnnis::PullRequest request;
    request.set_min_priority(tevnnis::HIGH);
    request.add_sectors("Semiconductor");

    grpc::ClientContext context;
    tevnnis::PullResponse response;
    REQUIRE(served.stub().PullDecisionBatch(&context, request, &response).ok());

    REQUIRE(EventIds(response).size() == 2);  // the CRITICAL move and the HIGH move
    for (const auto& event : response.events()) {
        REQUIRE(event.sector() == "Semiconductor");
        REQUIRE(event.priority() >= tevnnis::HIGH);
    }
}

TEST_CASE("WakeSignals is defined but unimplemented in v0", "[md][grpc]") {
    ServedPipeline served;

    grpc::ClientContext context;
    tevnnis::WakeRequest request;
    auto reader = served.stub().WakeSignals(&context, request);

    tevnnis::WakeEvent event;
    REQUIRE_FALSE(reader->Read(&event));
    const grpc::Status status = reader->Finish();
    REQUIRE(status.error_code() == grpc::StatusCode::UNIMPLEMENTED);
}
