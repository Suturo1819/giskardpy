from collections import namedtuple
from itertools import chain

from geometry_msgs.msg import Pose
from giskard_msgs.msg import WorldBody
import urdf_parser_py.urdf as up
from tf.transformations import euler_from_quaternion

from giskardpy.exceptions import DuplicateNameException
from giskardpy.utils import cube_volume, cube_surface, sphere_volume, cylinder_volume, cylinder_surface, \
    suppress_stderr, remove_outer_tag

Joint = namedtuple('Joint', ['symbol', 'velocity_limit', 'lower', 'upper', 'type', 'frame'])


def hacky_urdf_parser_fix(urdf_str):
    # TODO this function is inefficient but the tested urdf's aren't big enough for it to be a problem
    fixed_urdf = ''
    delete = False
    black_list = ['transmission', 'gazebo']
    black_open = ['<{}'.format(x) for x in black_list]
    black_close = ['</{}'.format(x) for x in black_list]
    for line in urdf_str.split('\n'):
        if len([x for x in black_open if x in line]) > 0:
            delete = True
        if len([x for x in black_close if x in line]) > 0:
            delete = False
            continue
        if not delete:
            fixed_urdf += line + '\n'
    return fixed_urdf


JOINT_TYPES = [u'fixed', u'revolute', u'continuous', u'prismatic']
MOVABLE_JOINT_TYPES = [u'revolute', u'continuous', u'prismatic']
ROTATIONAL_JOINT_TYPES = [u'revolute', u'continuous']
TRANSLATIONAL_JOINT_TYPES = [u'prismatic']


class URDFObject(object):
    def __init__(self, urdf):
        """
        :param urdf:
        :type urdf: str
        :param joints_to_symbols_map: maps urdf joint names to symbols
        :type joints_to_symbols_map: dict
        :param default_joint_vel_limit: all velocity limits which are undefined or higher than this will be set to this
        :type default_joint_vel_limit: Symbol
        """
        self.original_urdf = urdf
        with suppress_stderr():
            self._urdf_robot = up.URDF.from_xml_string(hacky_urdf_parser_fix(urdf))  # type: up.Robot

    @classmethod
    def from_urdf_file(cls, urdf_file):
        """
        :param urdf_file: path to urdf file
        :type urdf_file: str
        :param joints_to_symbols_map: maps urdf joint names to symbols
        :type joints_to_symbols_map: dict
        :param default_joint_vel_limit: all velocity limits which are undefined or higher than this will be set to this
        :type default_joint_vel_limit: float
        :rtype: up.Robot
        """
        with open(urdf_file, 'r') as f:
            urdf_string = f.read()
        self = cls(urdf_string, )
        return self

    @classmethod
    def from_world_body(cls, world_body):
        """
        :type world_body: giskard_msgs.msg._WorldBody.WorldBody
        :return:
        """
        links = []
        joints = []
        if world_body.type == world_body.PRIMITIVE_BODY or world_body.type == world_body.MESH_BODY:
            if world_body.shape.type == world_body.shape.BOX:
                geometry = up.Box(world_body.shape.dimensions)
            elif world_body.shape.type == world_body.shape.SPHERE:
                geometry = up.Sphere(world_body.shape.dimensions[0])
            elif world_body.shape.type == world_body.shape.CYLINDER:
                geometry = up.Cylinder(world_body.shape.dimensions)
            elif world_body.shape.type == world_body.shape.CONE:
                raise TypeError(u'primitive shape cone not supported')
            elif world_body.type == world_body.MESH_BODY:
                geometry = up.Mesh(world_body.mesh)
            else:
                raise TypeError(u'primitive shape \'{}\' not supported'.format(world_body.shape.type))
            link = up.Link(world_body.name,
                           visual=up.Visual(geometry, material=up.Material(u'green', color=up.Color(0, 1, 0, 1))),
                           collision=up.Collision(geometry))
            links.append(link)
        elif world_body.type == world_body.URDF_BODY:
            return cls(world_body.urdf)
        else:
            raise TypeError(u'world body type \'{}\' not supported'.format(world_body.type))
        return URDFObject.from_parts(world_body.name, links, joints)

    @classmethod
    def from_parts(cls, robot_name, links, joints):
        r = up.Robot(robot_name)
        for link in links:
            r.add_link(link)
        for joint in joints:
            r.add_joint(joint)
        return cls(r.to_xml_string())

    def get_name(self):
        """
        :rtype: str
        """
        return self._urdf_robot.name

    # JOINT FUNCITONS

    def get_joint_names(self):
        """
        :rtype: list
        """
        return self._urdf_robot.joint_map.keys()

    def get_joint_names_from_chain(self, root_link, tip_link):
        """
        :rtype root: str
        :rtype tip: str
        :rtype: list
        """
        return self._urdf_robot.get_chain(root_link, tip_link, True, False, True)

    def get_joint_names_from_chain_controllable(self, root_link, tip_link):
        """
        :rtype root: str
        :rtype tip: str
        :rtype: list
        """
        return self._urdf_robot.get_chain(root_link, tip_link, True, False, False)

    def get_joint_names_controllable(self):
        """
        :return: returns the names of all movable joints which are not mimic.
        :rtype: list
        """
        return [joint_name for joint_name in self.get_joint_names() if self.is_joint_controllable(joint_name)]

    def get_all_joint_limits(self):
        """
        :return: dict mapping joint names to tuple containing lower and upper limits
        :rtype: dict
        """
        return {joint_name: self.get_joint_limits(joint_name) for joint_name in self.get_joint_names()
                if self.is_joint_controllable(joint_name)}

    def get_joint_limits(self, joint_names):
        """
        Returns joint limits specified in the safety controller entry if given, else returns the normal limits.
        :param joint_name: name of the joint in the urdf
        :type joint_names: str
        :return: lower limit, upper limit or None if not applicable
        :rtype: float, float
        """
        joint = self.get_urdf_joint(joint_names)
        try:
            return max(joint.safety_controller.soft_lower_limit, joint.limit.lower), \
                   min(joint.safety_controller.soft_upper_limit, joint.limit.upper)
        except AttributeError:
            try:
                return joint.limit.lower, joint.limit.upper
            except AttributeError:
                return None, None

    def is_joint_controllable(self, name):
        """
        :param name: name of the joint in the urdf
        :type name: str
        :return: True if joint type is revolute, continuous or prismatic
        :rtype: bool
        """
        joint = self.get_urdf_joint(name)
        return joint.type in MOVABLE_JOINT_TYPES and joint.mimic is None

    def is_joint_mimic(self, name):
        """
        :param name: name of the joint in the urdf
        :type name: str
        :rtype: bool
        """
        joint = self.get_urdf_joint(name)
        return joint.type in MOVABLE_JOINT_TYPES and joint.mimic is not None

    def is_joint_continuous(self, name):
        """
        :param name: name of the joint in the urdf
        :type name: str
        :rtype: bool
        """
        return self.get_joint_type(name) == u'continuous'

    def get_joint_type(self, name):
        return self.get_urdf_joint(name).type

    def is_joint_type_supported(self, name):
        return self.get_joint_type(name) in JOINT_TYPES

    def is_rotational_joint(self, name):
        return self.get_joint_type(name) in ROTATIONAL_JOINT_TYPES

    def is_translational_joint(self, name):
        return self.get_joint_type(name) in TRANSLATIONAL_JOINT_TYPES

    # LINK FUNCTIONS

    def get_link_names_from_chain(self, root_link, tip_link):
        """
        :type root_link: str
        :type tip_link: str
        :return: list of all links in chain excluding root_link, including tip_link
        :rtype: list
        """
        return self._urdf_robot.get_chain(root_link, tip_link, False, True, False)

    def get_link_names(self):
        """
        :rtype: dict
        """
        return self._urdf_robot.link_map.keys()

    def get_sub_tree_link_names_with_collision(self, root_joint):
        """
        returns a set of links with
        :type: str
        :param volume_threshold: links with simple geometric shape and less volume than this will be ignored
        :type volume_threshold: float
        :param surface_treshold:
        :type surface_treshold: float
        :return: all links connected to root
        :rtype: list
        """
        sub_tree = self.get_links_from_sub_tree(root_joint)
        return [link_name for link_name in sub_tree if self.has_link_collision(link_name)]

    def get_links_from_sub_tree(self, joint_name):
        return self.get_sub_tree_at_joint(joint_name).get_link_names()

    def get_sub_tree_at_joint(self, joint_name):
        """
        :type joint_name: str
        :rtype: URDFObject
        """
        tree_links = []
        tree_joints = []
        joints = [joint_name]
        for joint in joints:
            child_link = self._urdf_robot.joint_map[joint].child
            if child_link in self._urdf_robot.child_map:
                for j, l in self._urdf_robot.child_map[child_link]:
                    joints.append(j)
                    tree_joints.append(self.get_urdf_joint(j))
            tree_links.append(self.get_urdf_link(child_link))

        return URDFObject.from_parts(joint_name, tree_links, tree_joints)

    def get_urdf_joint(self, joint_name):
        return self._urdf_robot.joint_map[joint_name]

    def get_urdf_link(self, link_name):
        return self._urdf_robot.link_map[link_name]

    def split_at_link(self, link_name):
        pass

    def has_link_collision(self, link_name, volume_threshold=1e-6, surface_threshold=1e-4):
        """
        :type link: str
        :param volume_threshold: m**3, ignores simple geometry shapes with a volume less than this
        :type volume_threshold: float
        :param surface_threshold: m**2, ignores simple geometry shapes with a surface area less than this
        :type surface_threshold: float
        :return: True if collision geometry is mesh or simple shape with volume/surface bigger than thresholds.
        :rtype: bool
        """
        link = self._urdf_robot.link_map[link_name]
        if link.collision is not None:
            geo = link.collision.geometry
            return isinstance(geo, up.Box) and (cube_volume(*geo.size) > volume_threshold or
                                                cube_surface(*geo.size) > surface_threshold) or \
                   isinstance(geo, up.Sphere) and sphere_volume(geo.radius) > volume_threshold or \
                   isinstance(geo, up.Cylinder) and (cylinder_volume(geo.radius, geo.length) > volume_threshold or
                                                     cylinder_surface(geo.radius, geo.length) > surface_threshold) or \
                   isinstance(geo, up.Mesh)
        return False

    def get_urdf(self):
        return self._urdf_robot.to_xml_string()

    def get_root(self):
        return self._urdf_robot.get_root()

    def attach_urdf_object(self, urdf_object, parent_link, pose):
        """
        Rigidly attach another object to the robot.
        :param urdf_object: Object that shall be attached to the robot.
        :type urdf_object: URDFObject
        :param parent_link_name: Name of the link to which the object shall be attached.
        :type parent_link_name: str
        :param pose: Hom. transform between the reference frames of the parent link and the object.
        :type pose: Pose
        """
        if urdf_object.get_name() in self.get_link_names():
            raise DuplicateNameException(
                u'\'{}\' already has link with name \'{}\'.'.format(self.get_name(), urdf_object.get_name()))
        if urdf_object.get_name() in self.get_joint_names():
            raise DuplicateNameException(
                u'\'{}\' already has joint with name \'{}\'.'.format(self.get_name(), urdf_object.get_name()))
        if parent_link not in self.get_link_names():
            raise KeyError(
                u'can not attach \'{}\' to non existent parent link \'{}\' of \'{}\''.format(urdf_object.get_name(),
                                                                                             parent_link,
                                                                                             self.get_name()))

        joint = up.Joint(self.robot_name_to_root_joint(urdf_object.get_name()),
                         parent=parent_link,
                         child=urdf_object.get_root(),
                         joint_type=u'fixed',
                         origin=up.Pose([pose.position.x,
                                         pose.position.y,
                                         pose.position.z],
                                        euler_from_quaternion([pose.orientation.x,
                                                               pose.orientation.y,
                                                               pose.orientation.z,
                                                               pose.orientation.w])))
        self._urdf_robot.add_joint(joint)
        for j in urdf_object._urdf_robot.joints:
            self._urdf_robot.add_joint(j)
        for l in urdf_object._urdf_robot.links:
            self._urdf_robot.add_link(l)
        self.reinitialize()
        pass

    def detach_sub_tree(self, joint_name):
        sub_tree = self.get_sub_tree_at_joint(joint_name)
        for link in sub_tree.get_link_names():
            self._urdf_robot.remove_aggregate(self.get_urdf_link(link))
        for joint in chain([joint_name], sub_tree.get_joint_names()):
            self._urdf_robot.remove_aggregate(self.get_urdf_joint(joint))
        self.reinitialize()

    def reinitialize(self):
        self._urdf_robot = up.URDF.from_xml_string(self.get_urdf())

    def robot_name_to_root_joint(self, name):
        # TODO should this really be a class function?
        return u'{}'.format(name)

    def get_parent_link_name(self, child_link_name):
        return self._urdf_robot.parent_map[child_link_name][1]

    def __eq__(self, o):
        """
        :type o: URDFObject
        :rtype: bool
        """
        return o.get_urdf() == self.get_urdf()