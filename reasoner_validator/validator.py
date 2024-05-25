from typing import Optional, List, Dict, Set, Tuple

from reasoner_validator.biolink import (
    BiolinkValidator,
    get_biolink_model_toolkit
)
from reasoner_validator.biolink.ontology import get_parent_concept

from reasoner_validator.report import TRAPIGraphType
from reasoner_validator.trapi import (
    LATEST_TRAPI_RELEASE,
    TRAPI_1_4_0_SEMVER,
    check_node_edge_mappings
)
from reasoner_validator.trapi.mapping import MappingValidator
from reasoner_validator.versioning import SemVer, SemVerError, get_latest_version
from reasoner_validator.utils import get_aliases

import logging
logger = logging.getLogger(__name__)


# Unspoken assumption here is that validation of results returned for
# Biolink Model release compliance only needs to be superficial
RESULT_TEST_DATA_SAMPLE_SIZE = 10


class TRAPIResponseValidator(BiolinkValidator):
    """
    TRAPIResponseValidator is an overall wrapper class for validating
    conformance of full TRAPI Responses to TRAPI and the Biolink Model.
    """
    def __init__(
            self,
            default_test: Optional[str] = None,
            default_target: Optional[str] = None,
            trapi_version: Optional[str] = None,
            biolink_version: Optional[str] = None,
            target_provenance: Optional[Dict[str, str]] = None,
            strict_validation: Optional[bool] = None,
            suppress_empty_data_warnings: bool = False
    ):
        """
        :param default_test: Optional[str] =  None, initial default test context of the TRAPIResponseValidator messages
        :param default_target: Optional[str] =  None, initial default target context of the TRAPIResponseValidator,
                                                also used as a prefix in validation messages.
        :param trapi_version: str, version of component against which to validate the message (mandatory, no default)
        :param biolink_version: Optional[str] = None, Biolink Model (SemVer) release against which the knowledge graph
                                is to be validated (Default: if None, use the Biolink Model Toolkit default version).
        :param strict_validation: Optional[bool] = None, if True, some tests validate as 'error';  False, simply issues
                                  'info' message; A value of 'None' uses the default value for specific graph contexts.
        :param suppress_empty_data_warnings: bool = False, validation normally reports empty Message query graph,
                                knowledge graph and results as warnings. This flag suppresses the reporting
                                of such warnings (default: False).
        """
        BiolinkValidator.__init__(
            self,
            default_test=default_test,
            default_target=default_target if default_target else "Validate TRAPI Response",
            trapi_version=trapi_version,
            biolink_version=biolink_version,
            target_provenance=target_provenance,
            strict_validation=strict_validation
        )
        self._is_trapi_1_4_or_later: Optional[bool] = None
        self.suppress_empty_data_warnings: bool = suppress_empty_data_warnings

    def is_trapi_1_4_or_later(self) -> bool:
        assert self.trapi_version
        try:  # try block ... Sanity check: in testcase the trapi_version is somehow invalid?
            target_major_version: SemVer = \
                SemVer.from_string(self.trapi_version, core_fields=['major', 'minor'],  ext_fields=[])
            self._is_trapi_1_4_or_later = target_major_version >= TRAPI_1_4_0_SEMVER
        except SemVerError as sve:
            logger.error(f"Current TRAPI release '{self.trapi_version}' seems invalid: {str(sve)}. Reset to latest?")
            self.trapi_version = LATEST_TRAPI_RELEASE
            self._is_trapi_1_4_or_later = True
        return self._is_trapi_1_4_or_later

    @staticmethod
    def sanitize_workflow(response: Dict) -> Dict:
        """
        Workflows in TRAPI Responses cannot be validated further due to missing tags and None values.
        This method is a temporary workaround to sanitize the query for additional validation.

        :param response: Dict full TRAPI Response JSON object
        :return: Dict, response with discretionary removal of content which
                       triggers (temporarily) unwarranted TRAPI validation failures
        """
        if 'workflow' in response and response['workflow']:
            # a 'workflow' is a list of steps, which are JSON object specifications
            workflow_steps: List[Dict] = response['workflow']
            for step in workflow_steps:
                if 'runner_parameters' in step and not step['runner_parameters']:
                    # self.report("warning.trapi.response.workflow.runner_parameters.missing")
                    step.pop('runner_parameters')
                if 'parameters' in step and not step['parameters']:
                    # There are some workflow types that have mandatory need for 'parameters'
                    # but this should be caught in a later schema validation step
                    # self.report("warning.trapi.response.workflow.parameters.missing")
                    step.pop('parameters')
        return response

    def check_compliance_of_trapi_response(
            self,
            response: Optional[Dict],
            max_kg_edges: int = 0,
            max_results: int = 0
    ):
        """
        One stop validation of all components of a TRAPI-schema compliant
        Query.Response, including its Message against a designated Biolink Model release.
        The high level structure of a Query.Response is described in
        https://github.com/NCATSTranslator/ReasonerAPI/blob/master/docs/reference.md#response-.

        The TRAPI Query.Response.Message is a Python Dictionary with three entries:

        * Query Graph ("QGraph"): knowledge graph query input parameters
        * Knowledge Graph: output knowledge (sub-)graph containing knowledge (Biolink Model compliant nodes, edges)
                           returned from the target resource (KP, ARA) for the query.
        * Results: a list of (annotated) node and edge bindings pointing into the Knowledge Graph, to represent the
                   specific answers (subgraphs) satisfying the query graph constraints.

        :param response: Optional[Dict], Query.Response to be validated.
        :param max_kg_edges: int, maximum number of edges to be validated from the
                                  knowledge graph of the response. A value of zero triggers validation
                                  of all edges in the knowledge graph (Default: 0 - use all edges)
        :param max_results: int, target sample number of results to validate (default: 0 for 'use all results').

        """
        if not (response and "message" in response):
            if not self.suppress_empty_data_warnings:
                self.report("error.trapi.response.empty")

            # nothing more to validate?
            return

        # Here, we split the TRAPI Response.Message out from the other
        # Response components, to allow for independent TRAPI Schema
        # validation of those non-Message components versus the Message
        # itself (checking along the way whether the Message is empty)
        message: Optional[Dict] = response.pop('message')

        # we insert a stub to enable TRAPI schema
        # validation of the remainder of the Response
        response['message'] = {}
        if message:

            # TRAPI JSON specified versions override default versions
            if "schema_version" in response and response["schema_version"]:
                if self.default_trapi:
                    self.trapi_version = get_latest_version(response["schema_version"])

            if "biolink_version" in response and response["biolink_version"]:
                if self.default_biolink:
                    self.bmt = get_biolink_model_toolkit(biolink_version=response["biolink_version"])
                    self.biolink_version = self.bmt.get_model_version()

            response = self.sanitize_workflow(response)

            self.is_valid_trapi_query(instance=response, component="Response")
            if not self.has_critical():

                status: Optional[str] = response['status'] if 'status' in response else None
                if status and status not in ["OK", "Success", "QueryNotTraversable", "KPsNotAvailable"]:
                    self.report("warning.trapi.response.status.unknown", identifier=status)

                # Sequentially validate the Query Graph, Knowledge Graph then validate
                # the Results (which rely on the validity of the other two components)
                elif self.has_valid_query_graph(message) and \
                        self.has_valid_knowledge_graph(message, max_kg_edges):
                    self.has_valid_results(message, max_results)

            # else:
            #     we don't validate further if it has
            #     critical Response level errors

        else:
            # Empty Message is valid TRAPI but reported as an error
            # in the validation and not interesting for further validation
            if not self.suppress_empty_data_warnings:
                self.report("error.trapi.response.message.empty")

        # Reconstitute the original Message
        # to the Response before returning
        response['message'] = message

    @staticmethod
    def sample_results(results: List, sample_size: int = 0) -> List:
        """
        Subsample the results to a maximum size of 'sample_size'

        :param results: List, original list of Results
        :param sample_size: int, target sample size (default: 0 for 'use all results').

        :return: List, 'sample_size' sized subset of Results
        """
        if sample_size > 0:
            sample_size = min(sample_size, len(results))
            return results[0:sample_size]
        else:
            return results

    @staticmethod
    def sample_graph(graph: Dict, edges_limit: int = 0) -> Dict:
        """
        Only process a strict subsample of the TRAPI Response Message knowledge graph.

        :param graph: original knowledge graph
        :type graph: Dict
        :param edges_limit: integer maximum number of edges to be validated in the knowledge graph. A value of zero
                            triggers validation of all edges in the knowledge graph (Default: 0 - use all edges)
        :type edges_limit: int

        :return: Dict, 'edges_limit' sized subset of knowledge graph
        """
        # We don't check for non-empty graphs here simply because the sole caller of this
        # method is the 'has_valid_knowledge_graph' method, which filters out empty graphs
        if edges_limit > 0:
            kg_sample: Dict = {
                "nodes": dict(),
                "edges": dict()
            }
            # 'sample_size' will always be a positive number here.
            # The kg_sample size will always be the 'sample_size'
            # or less, the latter situation arising if the number
            # of graph edges is smaller or some subject or
            # object ids are missing in the nodes list.
            sample_size = min(edges_limit, len(graph["edges"]))
            n = 0
            for key, edge in graph['edges'].items():

                kg_sample['edges'][key] = edge

                if 'subject' in edge and \
                        edge['subject'] in graph['nodes'] and \
                        edge['subject'] not in kg_sample['nodes']:
                    kg_sample['nodes'][edge['subject']] = graph['nodes'][edge['subject']]

                if 'object' in edge and \
                        edge['object'] in graph['nodes'] and \
                        edge['object'] not in kg_sample['nodes']:
                    kg_sample['nodes'][edge['object']] = graph['nodes'][edge['object']]

                n += 1
                if n == sample_size:
                    break

            return kg_sample

        else:
            # No pruning... just return the contents of the entire knowledge graph
            return {
                "nodes": graph["nodes"],
                "edges": graph["edges"]
            }

    def has_valid_query_graph(self, message: Dict) -> bool:
        """
        Validate a TRAPI Query Graph.
        :param message: input message expected to contain the 'query_graph'
        :return: bool, False, if validation errors
        """
        # Query Graph must not be missing...
        if 'query_graph' not in message:
            if not self.suppress_empty_data_warnings:
                self.report(code="error.trapi.response.message.query_graph.missing")
        else:
            # ... nor empty
            query_graph = message['query_graph']
            if not (query_graph and len(query_graph) > 0):
                if not self.suppress_empty_data_warnings:
                    self.report(code="error.trapi.response.message.query_graph.empty")
            else:
                # Validate the TRAPI compliance of the Query Graph
                self.is_valid_trapi_query(instance=query_graph, component="QueryGraph")

                if self.validate_biolink():
                    # Conduct validation of Biolink Model compliance
                    # of the Query Graph, if not suppressed...
                    self.check_biolink_model_compliance(query_graph, graph_type=TRAPIGraphType.Query_Graph)

        # Only 'error' but not 'info' nor 'warning' messages invalidate the overall Message
        return False if self.has_errors() else True

    def has_valid_knowledge_graph(
            self,
            message: Dict,
            edges_limit: int = 0
    ) -> bool:
        """
        Validate a TRAPI Knowledge Graph.

        :param message: Dict, input message expected to contain the 'knowledge_graph'
        :param edges_limit: int, integer maximum number of edges to be validated in the knowledge graph. A value of zero
                            triggers validation of all edges in the knowledge graph (Default: 0 - use all edges)

        :return: bool, False, if validation errors
        """
        # This integrity constraint may not really be necessary
        # since negative numbers are functionally equivalent to zero
        assert edges_limit >= 0, "The 'edges_limit' must be zero or a positive integer!"

        # The Knowledge Graph should not be missing
        if 'knowledge_graph' not in message:
            if not self.suppress_empty_data_warnings:
                self.report(code="error.trapi.response.message.knowledge_graph.missing")
        else:
            knowledge_graph = message['knowledge_graph']
            # The Knowledge Graph should also not generally be empty? Issue a warning
            if not (
                    knowledge_graph and len(knowledge_graph) > 0 and
                    "nodes" in knowledge_graph and knowledge_graph["nodes"] and
                    "edges" in knowledge_graph and knowledge_graph["edges"]
            ):
                # An empty knowledge graph (warning) does not generally invalidate
                # the whole Message, but no more validation tests are needed
                if not self.suppress_empty_data_warnings:
                    self.report(code="warning.trapi.response.message.knowledge_graph.empty")
            else:
                mapping_validator: MappingValidator = check_node_edge_mappings(knowledge_graph)
                if mapping_validator.has_messages():
                    self.merge(mapping_validator)

                # ...then if not empty, validate a subgraph sample of the associated
                # Knowledge Graph (since some TRAPI response kg's may be huge!)
                kg_sample = self.sample_graph(graph=knowledge_graph, edges_limit=edges_limit)

                # Verify that the sample of the knowledge graph is TRAPI compliant
                self.is_valid_trapi_query(instance=kg_sample, component="KnowledgeGraph")

                if self.validate_biolink():
                    # Conduct validation of Biolink Model compliance of the
                    # Knowledge Graph, if Biolink validation not suppressed...
                    self.check_biolink_model_compliance(
                        graph=kg_sample,
                        graph_type=TRAPIGraphType.Knowledge_Graph
                    )

        # Only 'error' but not 'info' nor 'warning'
        # messages invalidate the overall Message
        return False if self.has_errors() else True

    def has_valid_results(self, message: Dict, sample_size: int = 0) -> bool:
        """
        Validate a TRAPI Results.

        :param message: input message expected to contain the 'results'
        :param sample_size: int, sample number of results to validate (default: 0 for 'use all results').

        :return: bool, False, if validation errors
        """

        #     :param output_element: test target, as edge 'subject' or 'object'
        #     :type output_element: str
        #     :param output_node_binding: node 'a' or 'b' of the ('one hop') QGraph test query
        #     :type output_node_binding: str

        # The Message.Results key should not be missing?
        if 'results' not in message:
            if not self.suppress_empty_data_warnings:
                self.report(code="error.trapi.response.message.results.missing")
        else:
            results = message['results']

            if not (results and len(results) > 0):
                if not self.suppress_empty_data_warnings:
                    self.report(code="warning.trapi.response.message.results.empty")
                    # An empty result (warning) does not generally invalidate
                    # the whole Message, but no more validation tests are needed

            elif not isinstance(results, List):
                # The Message.results should be an array of Result objects
                self.report(code="error.trapi.response.message.results.not_array")

            else:
                # Validate a subsample of a non-empty Message.results component.
                results_sample = self.sample_results(results, sample_size=sample_size)
                for result in results_sample:

                    # generally validate against the pertinent schema
                    self.is_valid_trapi_query(instance=result, component="Result")

                    # Maybe some additional TRAPI-release specific non-schematic validation here?
                    if self.is_trapi_1_4_or_later():
                        # TODO: implement me!
                        pass
                    else:
                        pass

                    # TODO: here, we could try to compare the Results against the contents of the KnowledgeGraph,
                    #       with respect to node input values from the QueryGraph, but this is tricky to do solely
                    #       with the subsamples, which may not completely overlap,
                    #       and may also be somewhat computationally intensive?

                    # ...Finally, check that the sample Results contained the object of the Query

                    # The 'output_element' is 'subject' or 'object' target (unknown) of retrieval
                    # The 'output_node_binding' is (subject) 'a' or (object) 'b' keys in
                    # the QueryGraph.Nodes to be bound
                    # In principle, we detect which node in the QueryGraph has 'ids' associated with its node record
                    # and assume that the other edge node is the desired target (in the OneHop), so the 'ids'
                    # there should be in the output

                    # object_ids = [r['node_bindings'][output_node_binding][0]['id'] for r in results_sample]
                    # if testcase[output_element] not in object_ids:
                    #     # The 'get_aliases' method uses the Translator NodeNormalizer to check if any of
                    #     # the aliases of the testcase[output_element] identifier are in the object_ids list
                    #     output_aliases = get_aliases(testcase[output_element])
                    #     if not any([alias == object_id for alias in output_aliases for object_id in object_ids]):
                    #         validator.report(
                    #             code=error.results.missing_bindings,
                    #             identifier=testcase[output_element],
                    #             output_node_binding=output_node_binding
                    #         )
                    #         # data_dump=f"Resolved aliases:\n{','.join(output_aliases)}\n" +
                    #         #           f"Result object IDs:\n{_output(object_ids,flat=True)}"

        # Only 'error' but not 'info' nor 'warning' messages invalidate the overall Message
        return False if self.has_errors() else True

    def category_matched(self, source_categories: List[str], target_categories: List[str]) -> Optional[str]:
        """
        For each 'source' Biolink Model category given (list of CURIEs as strings?),
        first get the union set of all parent (ancestral) categories, then check if
        at least one of these categories is matched to the list of target categories.

        :param source_categories: List[str], list of 'source' categories whose category hierarchy is to be matched.
        :param target_categories: List[str], list of 'target' categories to be matched against 'source'
                                             (or 'source parent' categories
        :return: bool, returned category matched (could be a generic parent of a 'source' category)
        """
        source_category_set: Set = set()
        # gather all the possible exact and ancestor (parent)
        # categories of source_categories to match...
        for source_category in source_categories:
            source_category_set.update(self.bmt.get_ancestors(source_category, formatted=True, mixin=False))

        # ...then check all the target categories against that source category set
        for category in source_category_set:
            if category in target_categories:
                return category

        # Nothing matched
        return None

    def testcase_node_found(
            self,
            target: str,
            identifiers: List[str],
            testcase: Dict,
            nodes: Dict
    ) -> Optional[Tuple[str, str]]:
        """
        Check for presence of at least one of the given identifiers, with expected categories, in the "nodes" catalog.
        If such identifier is found, and at least one KG node category is the expected category or a proper subclass
        category of the test testcase category, then return True; if the node is found but the testcase category is not
        the expected category but is a subclass category of the KG node categories (i.e. KG node categories
        are too general), then return False. If the identifier is NOT found in the nodes list or
        there is no overlap in the (expected or parent) testcase and node categories, then return False.

        :param target: 'subject' or 'object'
        :param identifiers: List of node (CURIE) identifiers to be checked in the "nodes" catalog
        :param testcase: Dict, full test testcase (to access the target node 'category')
        :param nodes: Dict, details about knowledge graph nodes, indexed by node identifiers.
        :return: Optional[Tuple[str, str]], returns the identifier and category matched which could be an
                                            alias identifier and/or a generic (parent) category; None otherwise
        """
        #
        #     "nodes": {
        #         "MONDO:0005148": {"name": "type-2 diabetes"},
        #         "CHEBI:6801": {"name": "metformin", "categories": ["biolink:Drug"]}
        #     }
        #

        # Sanity check
        assert target in ["subject", "object"]
        for identifier in identifiers:
            if identifier in nodes.keys():
                # Found the target node identifier, but is the expected category present?
                # For this comparison, we assume that a specified node category plus all its
                # parent categories, may be used to match the test testcase specified category.
                node_details = nodes[identifier]
                test_case_category: str = testcase[f"{target}_category"]
                if "categories" in node_details:

                    category: Optional[str] = self.category_matched(
                            source_categories=node_details["categories"],
                            target_categories=[test_case_category]
                    )
                    if category is not None:
                        # The 'identifier' was present in the list of KG nodes, plus there was a match of the target
                        # testcase category either exactly to a specified one of the indicated KG node categories or to
                        # an ancestral ("parent") category of one of the node categories. This is a completely regular
                        # result. For example, if a KG node return is "biolink:Gene", but the testcase query is only
                        # expecting to see a "biolink:BiologicalEntity", however, since the identifier is matched
                        # exactly, since the node match is good with greater categorical precision than expected.
                        return identifier, category

                    # if a direct category match failed, try matching the inverse
                    category = self.category_matched(
                            source_categories=[test_case_category],
                            target_categories=node_details["categories"]
                    )
                    if category is not None:
                        # The 'identifier' was present in the list of KG nodes;
                        # however, there was likely only a more general ("parent-level") category match
                        # of at least one of the KG node categories, to a parent category of the testcase category
                        # (since the 'exact match' match scenario was handled above). This is a less precise
                        # node match, hence we issue a warning. For example, maybe the KG node returned is only
                        # tagged as "biolink:NamedThing" but we are looking for a testcase with "biolink:Gene".
                        # Since the testcase identifier was matched exactly, we assume that the node is matched,
                        # but just with less than desired data type categorical precision.
                        self.report(
                            code="warning.trapi.response.message.knowledge_graph.node.category.imprecise",
                            identifier=identifier,
                            expected_category=test_case_category,
                            observed_categories=",".join(node_details["categories"])
                        )
                        return identifier, category

        # Target node identifier is missing from the list of KG nodes or categories are either missing
        # or not annotated with any compatible category, hence, we deem the node effectively missing.
        return None

    @staticmethod
    def testcase_edge_bindings(q_edge_ids: List[str], target_edge_id: str, data: Dict) -> bool:
        """
        Check if target query edge id and knowledge graph edge id are in specified edge_bindings.
        :rtype: object
        :param q_edge_ids: List[str], expected query edge identifiers in a matching result
        :param target_edge_id:  str, expected knowledge edge identifier in a matching result
        :param data: TRAPI version-specific Response context from which the 'edge_bindings' may be retrieved
        :return: True, if found
        """
        edge_bindings: Dict = data["edge_bindings"]
        for bound_query_id, edge in edge_bindings.items():
            if bound_query_id in q_edge_ids:
                for binding_details in edge:
                    # TRAPI schema validation actually
                    # catches missing id's, but sanity check...
                    if "id" in binding_details:
                        if target_edge_id == binding_details["id"]:
                            return True
        return False

    def testcase_result_found(
            self,
            query_graph: Dict,
            subject_id: str,
            object_id: str,
            edge_id: str,
            results: List
    ) -> bool:
        """
        Validate that test testcase S--P->O edge is found bound to the Results?
        :param query_graph: Dict, query graph to which the results pertain
        :param subject_id: str, subject node (CURIE) identifier
        :param object_id:  str, subject node (CURIE) identifier
        :param edge_id:  str, subject node (CURIE) identifier
        :param results: List of (TRAPI-version specific) Result objects
        :return: bool, True if testcase S-P-O edge was found in the results
        """
        assert query_graph, "testcase_result_found() encountered an empty query graph"
        q_edges: Dict = query_graph["edges"]
        q_edge_ids = list(q_edges.keys())

        result_found: bool = False
        result: Dict

        for result in results:

            # Node binding validation still currently same for recent TRAPI versions
            node_bindings: Dict = result["node_bindings"]
            subject_id_found: bool = False
            object_id_found: bool = False
            edge_id_found: bool = False
            for node in node_bindings.values():
                for details in node:
                    if "id" in details:
                        if subject_id == details["id"]:
                            subject_id_found = True
                        if object_id == details["id"]:
                            object_id_found = True

            # However, TRAPI 1.4.0 Message 'Results' 'edge_bindings' are reported differently
            #          from 1.3.0, rather, embedded in 'Analysis' objects (and 'Auxiliary Graphs')
            if self.is_trapi_1_4_or_later():
                #
                #     "auxiliary_graphs": {
                #         "a0": {
                #             "edges": [
                #                 "e02",
                #                 "e12"
                #             ]
                #         },
                #         "a1": {
                #             "edges": [
                #                 "extra_edge0"
                #             ]
                #         },
                #         "a2": {
                #             "edges" [
                #                 "extra_edge1"
                #             ]
                #         }
                #     },
                #     "results": [
                #         # Single result in list:
                #         {
                #             "node_bindings": {
                #                 "n0": [
                #                     "id": "diabetes"
                #                 ],
                #                 "n1": [
                #                     "id": "metformin"
                #                 ]
                #             },
                #             "analyses":[
                #                 {
                #                     "reasoner_id": "ara0",
                #                     "edge_bindings": {
                #                         "e0": [
                #                             {
                #                                 "id": "e01"
                #                             },
                #                             {
                #                                 "id": "creative_edge"
                #                             }
                #                         ]
                #                     },
                #                     "support_graphs": [
                #                         "a1",
                #                         "a2"
                #                     ]
                #                     "score": ".7"
                #                 },
                #             ]
                #         }
                #     ]

                # result["analyses"] may be empty but prior TRAPI 1.4.0 schema validation ensures that
                # the "analysis" key is at least present plus the objects themselves are 'well-formed'
                analyses: List = result["analyses"]
                for analysis in analyses:
                    edge_id_found: bool = self.testcase_edge_bindings(q_edge_ids, edge_id, analysis)
                    if edge_id_found:
                        break

            else:
                # TRAPI 1.3.0 or earlier?
                #
                # Then, the TRAPI 1.3.0 Message Results (referencing the
                # Response Knowledge Graph) could be something like this:
                #
                #     "results": [
                #         # Single result in list:
                #         {
                #             "node_bindings": {
                #                # node "id"'s in knowledge graph, in edge "id"
                #                 "type-2 diabetes": [{"id": "MONDO:0005148"}],
                #                 "drug": [{"id": "CHEBI:6801"}]
                #             },
                #             "edge_bindings": {
                #                 # the edge binding key should be the query edge id
                #                 # bounded edge "id" is from knowledge graph
                #                 "treated_by": [{"id": "df87ff82"}]
                #             }
                #         }
                #     ]
                #
                edge_id_found: bool = self.testcase_edge_bindings(q_edge_ids, edge_id, result)

            if subject_id_found and object_id_found and edge_id_found:
                result_found = True
                break

        return result_found

    def resolve_testcase_node(
            self,
            target: str,
            testcase: Dict,
            nodes: Dict
    ) -> Optional[Tuple[str, str]]:
        """
        Resolve the knowledge graph nodes identifiers against the testcase identifier
        of the 'target' context ('subject' or 'object' node). If a direct
        match is not found for the testcase identifier, check if the nodes
        identifiers returned in the knowledge graph are strict ontological
        subclasses of the target testcase identifier (e.g. the knowledge graph
        may return a subclass of the MONDO disease requested by the testcase)
        Node matches must also be compatible in terms of Biolink Model category.

        :param target: 'subject' or 'object'
        :param testcase: Dict, full test testcase (to access the target node 'category')
        :param nodes: Dict, details about knowledge graph nodes, indexed by node identifiers
        :return: Optional[Tuple[str, str]], a 2-Tuple of node identifier matched and category matched; 'None' otherwise
        """
        target_id: str = testcase[f"{target}_id"] if f"{target}_id" in testcase else testcase[target]
        target_aliases = get_aliases(target_id)
        match: Optional[Tuple[str, str]] = self.testcase_node_found(target, target_aliases, testcase, nodes)
        if not match:
            # Maybe try to find an ontology parent to the
            # current 'target_id' then retry the match?
            parent_of_target_id: Optional[str] = get_parent_concept(
                curie=target_id,
                category=testcase[f"{target}_category"],
                biolink_version=self.get_biolink_version()
            )
            parent_of_target_aliases = get_aliases(parent_of_target_id) if parent_of_target_id else None
            if not parent_of_target_aliases:
                # no aliases, can't any further resolution of the node
                return None
            else:
                match = self.testcase_node_found(target, parent_of_target_aliases, testcase, nodes)
                if not match:
                    self.report(
                        code="error.trapi.response.message.knowledge_graph.node.missing",
                        identifier=target_id,
                        context=target
                    )
                    return None
                # else: we have an indirect match?
        # else: we have a direct match!

        # matched identifier, category and list of associated aliases
        return match

    def testcase_input_found_in_response(
            self,
            testcase: Dict,
            response: Dict
    ) -> bool:
        """
        Predicate to validate if test data test case specified edge is returned
        in the Knowledge Graph of the TRAPI Response Message. This method assumes
        that the TRAPI response is already generally validated as well-formed.

        :param testcase: Dict, input data test case
        :param response: Dict, TRAPI Response whose message ought to contain the test case edge
        :return: True if test case edge found; False otherwise
        """
        # sanity checks
        assert testcase, "testcase_input_found_in_response(): Empty or missing test testcase data!"
        assert response, "testcase_input_found_in_response(): Empty or missing TRAPI Response!"
        assert "message" in response, "testcase_input_found_in_response(): TRAPI Response missing its Message component!"

        #
        # testcase: Dict parameter contains something like:
        #
        #     idx: 0,
        #     subject_category: 'biolink:SmallMolecule',
        #     object_category: 'biolink:Disease',
        #     predicate: 'biolink:treats',
        #     subject_id: 'CHEBI:3002',  # may have the deprecated key 'subject' here
        #     object_id: 'MESH:D001249', # may have the deprecated key 'object' here
        #
        # the contents for which ought to be returned in
        # the TRAPI Knowledge Graph, with a Result mapping?
        #
        message: Dict = response["message"]

        # Another sanity check - unlikely to be a problem since
        # TRAPI schema validation should have picked it up since
        # the TRAPI Message is "nullable: false" in the schema
        assert message, "testcase_input_found_in_response(): Empty or missing TRAPI message component!"

        message_found: bool = False
        if "query_graph" not in message:
            # missing query graph allowed by the TRAPI schema but
            # the input test data edge is automatically deemed missing
            self.report(
                code="error.trapi.response.message.query_graph.missing"
            )
        elif not message["query_graph"]:
            # empty query graph is also allowed by the TRAPI schema
            # but the input test data edge is automatically deemed missing
            self.report(
                code="error.trapi.response.message.query_graph.empty"
            )
        elif "knowledge_graph" not in message:
            # missing knowledge graph allowed by the TRAPI schema but
            # the input test data edge is automatically deemed missing
            self.report(
                code="error.trapi.response.message.knowledge_graph.missing"
            )
        elif not message["knowledge_graph"]:
            # empty knowledge graph is also allowed by the TRAPI schema
            # but the input test data edge is automatically deemed missing
            self.report(
                code="error.trapi.response.message.knowledge_graph.empty"
            )
        elif "results" not in message:
            # missing results are allowed by the TRAPI schema but
            # the input test data edge is automatically deemed missing
            self.report(
                code="error.trapi.response.message.results.missing"
            )
        elif not message["results"]:
            # missing results are allowed by the TRAPI schema but
            # the input test data edge are automatically deemed missing
            self.report(
                code="error.trapi.response.message.results.empty"
            )
        else:
            message_found = True

        if not message_found:
            return False

        # The Message Query Graph could be something like:
        # "query_graph": {
        #     "nodes": {
        #         "type-2 diabetes": {"ids": ["MONDO:0005148"]},
        #         "drug": {"categories": ["biolink:Drug"]}
        #     },
        #     "edges": {
        #         "treated_by": {
        #             "subject": "type-2 diabetes",
        #             "predicates": ["biolink:treated_by"],
        #             "object": "drug"
        #         }
        #     }
        # }

        query_graph: Dict = message["query_graph"]

        #
        # with a Response Message Knowledge Graph
        # dictionary with 'nodes' and 'edges':
        #
        # "knowledge_graph": {
        #     "nodes": ...,
        #     "edges": ...
        # }
        knowledge_graph: Dict = message["knowledge_graph"]

        # In the Knowledge Graph:
        #
        #     "nodes": {
        #         "MONDO:0005148": {"name": "type-2 diabetes"},
        #         "CHEBI:6801": {"name": "metformin", "categories": ["biolink:Drug"]}
        #     }
        #

        # Check for testcase 'subject_id' and 'object_id',
        # with expected categories, in nodes catalog
        nodes: Dict = knowledge_graph["nodes"]

        subject_node_match: Optional[Tuple[str, str]] = \
            self.resolve_testcase_node(target="subject", testcase=testcase, nodes=nodes)
        if not subject_node_match:
            return False
        subject_match, subject_category_match = subject_node_match

        object_node_match: Optional[Tuple[str, str]] = \
            self.resolve_testcase_node(target="object", testcase=testcase, nodes=nodes)
        if not object_node_match:
            return False
        object_match, object_category_match = object_node_match

        # In the Knowledge Graph:
        #
        #     "edges": {
        #         "df87ff82": {
        #             "subject": "CHEBI:6801",
        #             "predicate": "biolink:treats",
        #             "object": "MONDO:0005148"
        #         }
        #     }
        #
        # Check in the edges catalog for an edge containing
        # the testcase 'subject_id', 'predicate' and 'object_id'
        edges: Dict = knowledge_graph["edges"]

        predicate = testcase["predicate"] if "predicate" in testcase else testcase["predicate_id"]
        predicate_descendants: List[str]
        inverse_predicate_descendants: List[str] = list()  # may sometimes remain empty...
        if self.validate_biolink():
            predicate_descendants = self.bmt.get_descendants(predicate, formatted=True)
            inverse_predicate = self.get_inverse_predicate(predicate)
            if inverse_predicate:
                inverse_predicate_descendants = self.bmt.get_descendants(inverse_predicate, formatted=True)
        else:
            # simpler testcase in which we are
            # ignoring deep Biolink Model validation
            predicate_descendants = [predicate]

        edge_id_match: Optional[str] = None
        edge_subject_match: Optional[str] = None
        edge_object_match: Optional[str] = None
        for testcase_edge_id, edge in edges.items():
            # Note: this edge search could be arduous on a big knowledge graph?
            if edge["subject"] == subject_match and \
                    edge["predicate"] in predicate_descendants and \
                    edge["object"] in object_match:
                edge_id_match = testcase_edge_id
                edge_subject_match = edge["subject"]
                edge_object_match = edge["object"]
                break
            elif edge["subject"] in object_match and \
                    edge["predicate"] in inverse_predicate_descendants and \
                    edge["object"] in subject_match:
                # observation of the inverse edge is also counted as a match?
                edge_subject_match = edge["subject"]
                edge_object_match = edge["object"]
                edge_id_match = testcase_edge_id
                break

        testcase_edge_id: str = \
            f"{testcase['idx']}|" +\
            f"({testcase['subject_id']}#{testcase['subject_category']})" + \
            f"-[{predicate}]->" + \
            f"({testcase['object_id']}#{testcase['object_category']})"

        if edge_id_match is None:
            self.report(
                code="error.trapi.response.message.knowledge_graph.edge.missing",
                identifier=testcase_edge_id
            )
            return False

        results: List = message["results"]
        if not self.testcase_result_found(query_graph, edge_subject_match, edge_object_match, edge_id_match, results):
            self.report(
                code="error.trapi.response.message.result.missing",
                identifier=testcase_edge_id
            )
            return False

        # By this point, the testcase data assumed to be
        # successfully validated in the TRAPI Response?
        return True
