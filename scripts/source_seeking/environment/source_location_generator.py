import numpy as np

def is_far_enough(new_point, existing_points, min_distance=2):
    """检查新点与现有点集是否保持足够距离"""
    for point in existing_points:
        if np.linalg.norm(new_point - point) < min_distance:
            return False
    return True

def generate_points(num_sets=10, points_per_set=5, min_distance=3, space_size=10):
    """生成符合条件的点集"""
    sets = []
    for _ in range(num_sets):
        points = []
        while len(points) < points_per_set:
            point = np.random.uniform(1, space_size - 1, 2)
            if len(points) == 0 or is_far_enough(point, points, min_distance):
                points.append(point)
        
        points = np.round(np.array(points) * 2) / 2
        sets.append(points)
    return sets



def format_points(sets_of_points):
    """
    Formats the sets of points into a string representation.

    :param sets_of_points: List of sets of points
    :return: List of formatted strings for each set of points
    """
    formatted_sets = []
    print("SOURCES = np.array([", end="")
    for points in sets_of_points:
        print( str(points.tolist()), ",")
    print("])")
    return 

# Parameters
num_sets = 10
num_points = 5
min_value = 1  # Min integer value
max_value = 9  # Max integer value (exclusive)

# Generate the points
sets_of_points = generate_points()
format_points(sets_of_points)

